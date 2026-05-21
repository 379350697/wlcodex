"""Tests for Chief Engineer orchestrator."""

import subprocess

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

    assert result.status == "failed"
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


@pytest.mark.asyncio
async def test_orchestrator_does_not_implement_read_only_ops_diagnostic() -> None:
    """Read-only diagnostic + needs_implementation:true → needs_user, not passed.

    Codex must still NOT enter Claude, but the result must surface the fix
    list to the user instead of silently marking "passed".
    """
    codex = FakeCodexWithSendPrompt()
    codex._responses = [
        (
            "结论：下午部署已经生效，local L2 ready 有改善。\n"
            '{"summary":"后续可以修复残留指标",'
            '"needs_implementation":true,'
            '"files_to_touch":["lightfee/runtime.py"],'
            '"implementation_steps":["补充诊断"]}'
        )
    ]
    claude = FakeClaudeWithSend()

    orchestrator = ChiefEngineerOrchestrator(codex, claude)
    result = await orchestrator.run(
        "云服务器日志：请看下上一版部署后最近 local l2 ready 有改善吗，下午更新有生效吗"
    )

    assert result.status == "needs_user"
    assert result.verify_round == 0
    assert len(codex.prompts) == 1
    assert claude.prompts == []
    # Verification summary must be human-readable — no raw JSON keys
    assert "诊断完成" in result.verification_summary
    assert "需要你确认" in result.verification_summary
    assert '"summary"' not in result.verification_summary
    assert '"needs_implementation"' not in result.verification_summary


def test_verification_decision_parse_pass() -> None:
    d = VerificationDecision.parse("decision: pass\nsummary: All checks passed")
    assert d.decision == "pass"


def test_verification_decision_parse_retry() -> None:
    d = VerificationDecision.parse("decision: retry\nsummary: Need more tests\nrequired_fix: add edge case test")
    assert d.decision == "retry"


def test_verification_decision_parse_retry_overrides_negative_chinese_pass_phrase() -> None:
    d = VerificationDecision.parse(
        "decision: retry\n\n本次实现不能验收通过。相关测试不是全绿。"
    )
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


def _init_git_workspace(path) -> None:
    (path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (path / "docs").mkdir()
    (path / "docs" / "README.md").write_text("docs\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=WLCodex Tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-m",
            "init",
        ],
        cwd=path,
        check=True,
        capture_output=True,
    )


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
async def test_orchestrator_stops_if_codex_analysis_edits_code(tmp_path) -> None:
    _init_git_workspace(tmp_path)

    class MutatingCodex:
        async def send_codex_prompt(
            self,
            workspace_path: str,
            prompt: str,
            *,
            interaction_mode: str = "general",
        ) -> str:
            del prompt, interaction_mode
            (tmp_path / "app.py").write_text("print('changed')\n", encoding="utf-8")
            return "Root cause: app.py needs work."

    codex = MutatingCodex()
    claude = FakeClaudeWithSend()

    orchestrator = ChiefEngineerOrchestrator(codex, claude, max_verify_rounds=1)
    result = await orchestrator.run(
        "修改 app.py",
        {"workspace": str(tmp_path)},
    )

    assert result.status == "failed"
    assert "Codex 总工程师轮修改了实现文件" in result.verification_summary
    assert claude.prompts == []


@pytest.mark.asyncio
async def test_orchestrator_allows_codex_analysis_artifact_docs(tmp_path) -> None:
    _init_git_workspace(tmp_path)

    class DocWritingCodex:
        def __init__(self) -> None:
            self.calls = 0

        async def send_codex_prompt(
            self,
            workspace_path: str,
            prompt: str,
            *,
            interaction_mode: str = "general",
        ) -> str:
            del workspace_path, prompt, interaction_mode
            self.calls += 1
            if self.calls == 1:
                (tmp_path / "docs" / "codex-plan.md").write_text(
                    "# Plan\n",
                    encoding="utf-8",
                )
                return "Root cause: implementation needed."
            return "decision: pass\nsummary: verified"

    codex = DocWritingCodex()
    claude = FakeClaudeWithSend()

    orchestrator = ChiefEngineerOrchestrator(codex, claude, max_verify_rounds=1)
    result = await orchestrator.run(
        "实现一个小功能",
        {"workspace": str(tmp_path)},
    )

    assert result.status == "passed"
    assert (tmp_path / "docs" / "codex-plan.md").exists()
    assert len(claude.prompts) == 1


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
async def test_run_streaming_does_not_implement_read_only_ops_diagnostic() -> None:
    """Streaming: read-only diagnostic + needs_implementation:true → needs_user."""
    codex = FakeCodexWithSendPrompt()
    codex._responses = [
        (
            "结论：下午部署已经生效，local L2 ready 有改善。\n"
            '{"summary":"后续可以修复残留指标",'
            '"needs_implementation":true,'
            '"files_to_touch":["lightfee/runtime.py"],'
            '"implementation_steps":["补充诊断"]}'
        )
    ]
    claude = FakeClaudeStreaming()

    orchestrator = ChiefEngineerOrchestrator(codex, claude)
    events = []
    async for progress in orchestrator.run_streaming(
        "云服务器日志：请看下上一版部署后最近 local l2 ready 有改善吗，下午更新有生效吗"
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
    assert complete_events[0].result_status == "needs_user"
    assert complete_events[0].round_num == 0
    # full_text must be human-readable, no raw JSON
    full = complete_events[0].full_text or ""
    assert "诊断完成" in full
    assert "需要你确认" in full
    assert '"summary"' not in full


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


# ---------------------------------------------------------------------------
# Max verify rounds retry → FAILED convergence (Bug 3 fix)
# ---------------------------------------------------------------------------


class PersistentRetryCodex:
    """Fake Codex that always returns retry for verification."""

    def __init__(self) -> None:
        self.prompts: list[tuple[str, str]] = []
        self.call_count = 0

    async def send_codex_prompt(
        self, workspace_path: str, prompt: str, **kwargs
    ) -> str:
        self.prompts.append((workspace_path, prompt))
        self.call_count += 1
        if self.call_count == 1:
            return "Root cause: null check needed.\nfiles_to_touch: [auth.py]\nimplementation_steps: [add null check]"
        # All verification calls: always retry
        return "decision: retry\nsummary: Still missing tests.\nrequired_fix: Add edge case tests for null input."


@pytest.mark.asyncio
async def test_max_verify_rounds_with_persistent_retry_yields_failed() -> None:
    """When max verify rounds reached and Codex still says retry, yield FAILED + COMPLETE(failed)."""
    from wlcodex.orchestrator import OrchestrationProgress

    codex = PersistentRetryCodex()
    claude = FakeClaudeStreaming()

    orchestrator = ChiefEngineerOrchestrator(codex, claude, max_verify_rounds=3)
    events = []
    async for progress in orchestrator.run_streaming("修复登录 bug"):
        events.append(progress)

    phases = [e.phase for e in events]
    failed_events = [e for e in events if e.phase == OrchestrationProgress.FAILED]
    complete_events = [e for e in events if e.phase == OrchestrationProgress.COMPLETE]

    # Must have a FAILED event when max rounds reached
    assert failed_events, f"Expected FAILED event, got phases: {phases}"
    assert "最大验收轮次" in failed_events[0].text
    # COMPLETE must carry result_status='failed'
    assert complete_events
    assert complete_events[-1].result_status == "failed"
    # Must NOT end with passed
    assert not any(e.result_status == "passed" for e in complete_events)


@pytest.mark.asyncio
async def test_max_verify_rounds_does_not_start_extra_implementation() -> None:
    """After max verify rounds with retry, no additional Claude implementation should run."""
    codex = PersistentRetryCodex()
    claude = FakeClaudeStreaming()

    orchestrator = ChiefEngineerOrchestrator(codex, claude, max_verify_rounds=2)
    async for _ in orchestrator.run_streaming("修复登录 bug"):
        pass

    # Claude should be called exactly max_verify_rounds times (2), not more
    assert len(claude.prompts) == 2


# ---------------------------------------------------------------------------
# Claude implementation must not plan-only (Bug 2 fix)
# ---------------------------------------------------------------------------


def test_claude_handoff_packet_includes_implementation_instruction() -> None:
    """The Claude handoff packet must explicitly instruct immediate implementation."""
    from wlcodex.context_packets import build_claude_handoff_packet

    packet = build_claude_handoff_packet(
        user_goal="修复登录 bug",
        codex_analysis="Root cause: null check needed in auth.py",
    )
    rendered = packet.render()

    # Must include the implementation-phase constraint
    assert "实施阶段" in rendered or "implementation" in rendered.lower()
    assert "立即" in rendered or "immediately" in rendered.lower() or "不要输出计划" in rendered
    assert "diff" in rendered.lower() or "文件变更" in rendered or "实际" in rendered


# ---------------------------------------------------------------------------
# Problem 1 regression: read-only diagnostic short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_only_diagnostic_needs_impl_false_still_passes() -> None:
    """Read-only diagnostic + needs_implementation:false → passed (correct)."""
    codex = FakeCodexWithSendPrompt()
    codex._responses = [
        '{"summary":"一切正常","needs_implementation":false,'
        '"files_to_touch":[],"implementation_steps":[],"acceptance_criteria":[]}'
    ]
    claude = FakeClaudeWithSend()

    orchestrator = ChiefEngineerOrchestrator(codex, claude)
    result = await orchestrator.run(
        "云服务器：检查下服务状态是否正常"
    )

    assert result.status == "passed"
    assert result.verify_round == 0
    assert claude.prompts == []


@pytest.mark.asyncio
async def test_read_only_diagnostic_needs_impl_true_becomes_needs_user() -> None:
    """Read-only diagnostic + needs_implementation:true → needs_user, never Claude."""
    codex = FakeCodexWithSendPrompt()
    codex._responses = [
        '{"summary":"部署未生效",'
        '"needs_implementation":true,'
        '"files_to_touch":["deploy.sh"],'
        '"implementation_steps":["重新部署"],'
        '"acceptance_criteria":["服务版本号更新"]}'
    ]
    claude = FakeClaudeWithSend()

    orchestrator = ChiefEngineerOrchestrator(codex, claude)
    result = await orchestrator.run(
        "云服务器日志：查一下最近部署有没有生效"
    )

    assert result.status == "needs_user"
    assert result.verify_round == 0
    assert claude.prompts == []
    # No raw JSON in user-facing text
    summary = result.verification_summary
    assert '"summary"' not in summary
    assert '"needs_implementation"' not in summary
    assert '{' not in summary
    assert "诊断完成" in summary
    assert "需要你确认" in summary
    assert "部署未生效" in summary
    assert "deploy.sh" in summary
    assert "重新部署" in summary


@pytest.mark.asyncio
async def test_normal_implementation_needs_impl_true_still_enters_claude() -> None:
    """Normal chief-engineer task + needs_implementation:true → enters Claude."""
    codex = FakeCodexWithSendPrompt()
    codex._responses = [
        '{"summary":"需要修复登录bug",'
        '"needs_implementation":true,'
        '"files_to_touch":["auth.py"],'
        '"implementation_steps":["添加null检查"]}',
        "decision: pass\nsummary: verified",
    ]
    claude = FakeClaudeWithSend()

    orchestrator = ChiefEngineerOrchestrator(codex, claude, max_verify_rounds=1)
    result = await orchestrator.run("修复登录 bug")

    assert result.status == "passed"
    assert result.verify_round == 1
    assert claude.prompts != []


@pytest.mark.asyncio
async def test_reply_only_still_works_as_before() -> None:
    """Reply-only probe must still return exact text, no side effects."""
    codex = FakeCodexWithSendPrompt()
    codex._responses = ["wlcodex telegram live ok"]
    claude = FakeClaudeWithSend()

    orchestrator = ChiefEngineerOrchestrator(codex, claude)
    result = await orchestrator.run("请用中文只回复：wlcodex telegram live ok")

    assert result.status == "passed"
    assert result.verify_round == 0
    assert claude.prompts == []


# ---------------------------------------------------------------------------
# Problem 2 regression: concatenated JSON parsing and human-readable output
# ---------------------------------------------------------------------------


def test_parse_last_complete_json_single_object() -> None:
    from wlcodex.orchestrator import _parse_last_complete_json

    result = _parse_last_complete_json(
        '{"summary":"ok","needs_implementation":false}'
    )
    assert result == {"summary": "ok", "needs_implementation": False}


def test_parse_last_complete_json_concatenated() -> None:
    from wlcodex.orchestrator import _parse_last_complete_json

    result = _parse_last_complete_json(
        '{"summary":"first"}{"summary":"second","needs_implementation":true}'
    )
    assert result == {"summary": "second", "needs_implementation": True}


def test_parse_last_complete_json_trailing_text() -> None:
    from wlcodex.orchestrator import _parse_last_complete_json

    result = _parse_last_complete_json(
        '{"summary":"a"}\n一些中文说明\n{"summary":"final","needs_implementation":true}后面的文字'
    )
    assert result == {"summary": "final", "needs_implementation": True}


def test_parse_last_complete_json_incomplete_last_object() -> None:
    from wlcodex.orchestrator import _parse_last_complete_json

    result = _parse_last_complete_json(
        '{"summary":"first","needs_implementation":false}{"summary":"incomplete"'
    )
    assert result == {"summary": "first", "needs_implementation": False}


def test_parse_last_complete_json_no_json() -> None:
    from wlcodex.orchestrator import _parse_last_complete_json

    result = _parse_last_complete_json("只是中文文本，没有JSON")
    assert result is None


def test_visible_analysis_reply_extracts_summary_from_concatenated_json() -> None:
    from wlcodex.orchestration_runner import _visible_analysis_reply

    reply = _visible_analysis_reply(
        '{"summary":"中间分析"}{"summary":"最终结论：部署已生效，还需要修复残留指标"}'
    )
    assert "最终结论" in reply
    assert "中间分析" not in reply
    assert '{' not in reply
    assert '"summary"' not in reply


def test_visible_analysis_reply_handles_plain_text() -> None:
    from wlcodex.orchestration_runner import _visible_analysis_reply

    reply = _visible_analysis_reply("部署正常，无需修改。")
    assert reply == "部署正常，无需修改。"


def test_visible_analysis_reply_never_shows_json_keys() -> None:
    from wlcodex.orchestration_runner import _visible_analysis_reply

    for text in [
        '{"summary":"正常","needs_implementation":false}',
        '{"summary":"需要修复","needs_implementation":true,"files_to_touch":["a.py"]}',
        'prose{"summary":"最终","needs_implementation":true}',
    ]:
        reply = _visible_analysis_reply(text)
        assert '"needs_implementation"' not in reply
        assert '"files_to_touch"' not in reply
        assert '"implementation_steps"' not in reply


def test_render_diagnostic_needs_implementation_produces_no_raw_json() -> None:
    from wlcodex.orchestrator import _render_diagnostic_needs_implementation

    rendered = _render_diagnostic_needs_implementation(
        '{"summary":"需要重启服务",'
        '"needs_implementation":true,'
        '"files_to_touch":["deploy.sh","config.toml"],'
        '"implementation_steps":["SSH到服务器","执行重启"],'
        '"acceptance_criteria":["服务恢复","日志无报错"]}'
    )

    assert "诊断完成" in rendered
    assert "需要你确认" in rendered
    assert "需要重启服务" in rendered
    assert "deploy.sh" in rendered
    assert "config.toml" in rendered
    assert "SSH到服务器" in rendered
    assert "执行重启" in rendered
    assert "服务恢复" in rendered
    assert "日志无报错" in rendered
    # No raw JSON
    assert '"summary"' not in rendered
    assert '"needs_implementation"' not in rendered
    assert '{' not in rendered
