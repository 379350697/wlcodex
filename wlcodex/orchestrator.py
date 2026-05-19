"""Chief Engineer orchestration: Codex-Claude-Codex loop with verification retry.

Codex owns analysis, architecture, prompt shaping, verification, and closure.
Claude Code is the implementation engineer.
The orchestrator enforces compact context packets for every model call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import inspect
import json
import logging
from pathlib import Path
import subprocess
from typing import Any

from wlcodex.context_packets import (
    ContextBudget,
    build_claude_handoff_packet,
    build_codex_analysis_packet,
    build_codex_verification_packet,
)

logger = logging.getLogger(__name__)

_CODEX_ALLOWED_WRITE_PREFIXES = ("docs/", ".wlcodex/")


def _accepts_keyword(func: object, name: str) -> bool:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        or parameter.name == name
        for parameter in signature.parameters.values()
    )


# Patterns that indicate Claude or Codex attempted direct Telegram delivery
# or token access — these are violations of the platform isolation contract.
_DIRECT_TELEGRAM_SEND_PATTERNS: tuple[str, ...] = (
    "message_id=",
    "message_id =",
    "api.telegram.org",
    "sendMessage",
    "editMessageText",
    "sendChatAction",
    "editMessageReplyMarkup",
    "telegram.message.sent",
    "telegram bot sent",
    "已发送 Telegram",
    "已发送 telegram",
    "Telegram 已发送",
    "telegram 已发送",
    "via Telegram",
    "通过 Telegram",
    "已通过 Telegram",
    "curl.*telegram",
    "http.*telegram.*bot",
)

_DIRECT_TOKEN_ACCESS_PATTERNS: tuple[str, ...] = (
    "WLCODEX_TELEGRAM_BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "telegram.*bot_token",
    "bot.*token.*telegram",
    "os.environ.*TELEGRAM",
    "os.getenv.*TELEGRAM",
    "env.*TELEGRAM_BOT",
)

_NEGATED_DELIVERY_REFERENCE_MARKERS: tuple[str, ...] = (
    "未发现",
    "没有",
    "不会",
    "不要",
    "不能",
    "无法",
    "禁止",
    "not ",
    "no ",
    "never",
    "without",
    "did not",
    "does not",
    "do not",
    "must not",
    "should not",
    "will not",
    "won't",
)


def _has_non_negated_pattern(text: str, pattern: str) -> bool:
    """Return true when *pattern* appears outside a negated/audit context."""
    pattern_lower = pattern.lower()
    for line in text.splitlines() or [text]:
        lowered = line.lower()
        search_from = 0
        while True:
            idx = lowered.find(pattern_lower, search_from)
            if idx < 0:
                break
            window = lowered[max(0, idx - 90): idx + len(pattern_lower) + 90]
            if not any(
                marker.lower() in window
                for marker in _NEGATED_DELIVERY_REFERENCE_MARKERS
            ):
                return True
            search_from = idx + len(pattern_lower)
    return False


def _detect_claude_direct_delivery_drift(impl_text: str) -> list[str]:
    """Return a list of drift descriptions found in Claude's implementation text.

    Each item describes a specific violation (direct Telegram send claim or
    token access attempt).  Empty list means no drift detected.
    """
    findings: list[str] = []
    lowered = impl_text.lower()
    for pattern in _DIRECT_TELEGRAM_SEND_PATTERNS:
        if pattern.lower() in lowered:
            findings.append(
                f"Claude claimed direct Telegram delivery: matched '{pattern}'"
            )
            break  # one finding per category is enough
    for pattern in _DIRECT_TOKEN_ACCESS_PATTERNS:
        if pattern.lower() in lowered:
            findings.append(
                f"Claude attempted Telegram token access: matched '{pattern}'"
            )
            break
    return findings


def _detect_verification_delivery_drift(verify_text: str) -> list[str]:
    """Return a list of drift descriptions found in Codex verification text.

    Codex verification must not request tokens or attempt to send Telegram.
    """
    findings: list[str] = []
    lowered = verify_text.lower()
    for pattern in _DIRECT_TELEGRAM_SEND_PATTERNS:
        if pattern.lower() in lowered and _has_non_negated_pattern(verify_text, pattern):
            findings.append(
                f"Codex verification attempted direct delivery: matched '{pattern}'"
            )
            break
    for pattern in _DIRECT_TOKEN_ACCESS_PATTERNS:
        if pattern.lower() in lowered and _has_non_negated_pattern(verify_text, pattern):
            findings.append(
                f"Codex verification attempted token access: matched '{pattern}'"
            )
            break
    return findings


def _analysis_says_no_implementation_needed(text: str) -> bool:
    """Return true when Codex analysis says the request is informational/no-op."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and parsed.get("needs_implementation") is False:
        return True

    lowered = text.lower()
    markers = (
        "no code changes",
        "no implementation needed",
        "no implementation is needed",
        "no changes needed",
        "不需要修改",
        "无需修改",
        "无需实施",
        "无需实现",
        "无需编程",
        "无需代码变更",
        "无需实施计划",
        "没有具体故障",
        "没有具体任务",
        "暂无可分析的根因",
    )
    return any(marker in lowered or marker in text for marker in markers)


def _is_reply_only_request(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "只回复",
        "仅回复",
        "只回答",
        "仅回答",
        "reply exactly",
        "only reply",
        "respond exactly",
        "only respond",
    )
    return any(marker in lowered or marker in text for marker in markers)


def _collect_workspace_evidence(workspace_path: str) -> tuple[list[str], str, str]:
    """Collect changed files, diff stat, and test info from workspace.

    Returns (changed_files, diff_summary, test_results).
    Never raises — returns empty evidence on any failure.
    """
    changed_files: list[str] = []
    diff_summary = ""
    test_results = ""

    try:
        result = subprocess.run(
            ["git", "-C", workspace_path, "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            changed_files = [
                f.strip() for f in result.stdout.strip().split("\n") if f.strip()
            ]
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["git", "-C", workspace_path, "diff", "--stat", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            diff_summary = result.stdout.strip()
    except Exception:
        pass

    # Collect test evidence — note what we could and couldn't run
    try:
        result = subprocess.run(
            ["git", "-C", workspace_path, "diff", "HEAD", "--", "tests/", "test_"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            test_results = (
                "Tests were modified in this change. "
                "Test commands were NOT executed by the orchestrator — "
                "manual verification required. "
                "Run: pytest tests/ -q\n\n"
                f"Test file changes:\n{result.stdout.strip()[:800]}"
            )
        else:
            test_results = (
                "No test files were modified in this change. "
                "Manual verification of correctness is required."
            )
    except Exception:
        test_results = "Unable to determine test changes. Manual verification required."

    return changed_files, diff_summary, test_results


def _git_workspace_root(workspace_path: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", workspace_path, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    root = result.stdout.strip()
    return root or None


def _git_changed_file_paths(workspace_path: str) -> set[str] | None:
    paths: set[str] = set()
    commands = (
        ["ls-files", "-m", "-d", "-o", "--exclude-standard", "-z"],
        ["diff", "--cached", "--name-only", "-z"],
    )
    for args in commands:
        try:
            result = subprocess.run(
                ["git", "-C", workspace_path, *args],
                capture_output=True,
                timeout=15,
            )
        except Exception:
            return None
        if result.returncode != 0:
            return None
        paths.update(
            part.decode("utf-8", errors="replace")
            for part in result.stdout.split(b"\0")
            if part
        )
    return paths


def _file_signature(root: Path, relative_path: str) -> str:
    path = root / relative_path
    if not path.exists():
        return "<missing>"
    if path.is_dir():
        return "<directory>"
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception as exc:
        return f"<unreadable:{type(exc).__name__}>"


def _capture_workspace_snapshot(workspace_path: str) -> dict[str, str] | None:
    root = _git_workspace_root(workspace_path)
    if root is None:
        return None
    paths = _git_changed_file_paths(workspace_path)
    if paths is None:
        return None
    root_path = Path(root)
    return {path: _file_signature(root_path, path) for path in paths}


def _is_codex_allowed_write_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized.startswith(_CODEX_ALLOWED_WRITE_PREFIXES)


def _codex_forbidden_workspace_changes(
    before: dict[str, str] | None,
    after: dict[str, str] | None,
) -> list[str]:
    if before is None or after is None:
        return []
    changed_paths = sorted(
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    )
    return [
        path for path in changed_paths
        if not _is_codex_allowed_write_path(path)
    ]


@dataclass
class OrchestrationResult:
    status: str
    verify_round: int = 0
    codex_analysis: str = ""
    claude_implementation: str = ""
    verification_summary: str = ""
    steps: list[OrchestrationStepResult] = field(default_factory=list)


@dataclass
class OrchestrationStepResult:
    step: str
    agent: str
    summary: str


@dataclass
class OrchestrationProgress:
    phase: str  # analysis_started, analysis_complete, implementation_delta, etc.
    text: str = ""
    full_text: str = ""
    agent: str = ""
    result_status: str = ""  # passed, failed, needs_user — set on COMPLETE
    round_num: int = 0

    # Phase constants for controller dispatch
    ANALYSIS_STARTED = "analysis_started"
    ANALYSIS_COMPLETE = "analysis_complete"
    IMPL_DELTA = "implementation_delta"
    IMPL_COMPLETE = "implementation_complete"
    VERIFY_STARTED = "verification_started"
    VERIFY_COMPLETE = "verification_complete"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass
class VerificationDecision:
    decision: str  # pass, retry, stop, need_user
    summary: str = ""
    required_fix: str = ""
    confidence: str = ""

    @classmethod
    def parse(cls, text: str) -> "VerificationDecision":
        decision = "need_user"
        summary = text
        required_fix = ""

        text_lower = text.lower()
        if "decision: retry" in text_lower:
            decision = "retry"
            if "required_fix:" in text_lower:
                fix_idx = text_lower.find("required_fix:")
                required_fix = text[fix_idx:].split("\n")[0]
            else:
                required_fix = text
        elif "decision: pass" in text_lower:
            decision = "pass"
        elif "decision: stop" in text_lower:
            decision = "stop"
        elif "decision: need_user" in text_lower:
            decision = "need_user"
        elif "需要修改" in text or "不能验收通过" in text:
            decision = "retry"
            required_fix = text
        elif "无法完成" in text:
            decision = "stop"
        elif "需要用户" in text:
            decision = "need_user"
        elif "验收通过" in text:
            decision = "pass"

        return cls(decision=decision, summary=summary, required_fix=required_fix)


class ChiefEngineerOrchestrator:
    """Orchestrates the Codex-Claude-Codex verification loop."""

    def __init__(
        self,
        codex_backend: object,
        claude_backend: object,
        max_verify_rounds: int = 3,
        budget: ContextBudget | None = None,
        *,
        pending_user_context: str = "",
    ) -> None:
        self._codex = codex_backend
        self._claude = claude_backend
        self._max_verify_rounds = max_verify_rounds
        self._budget = budget or ContextBudget()
        self._last_claude_drift_findings: list[str] = []
        self._pending_user_context = pending_user_context

    def set_pending_user_context(self, context: str) -> None:
        """Set pending user context for the next verification round."""
        self._pending_user_context = context

    async def run(
        self,
        user_goal: str,
        conversation_context: dict[str, Any] | None = None,
    ) -> OrchestrationResult:
        ctx = conversation_context or {}
        workspace = ctx.get("workspace", "wlcodex")
        result = OrchestrationResult(status="running")

        # Step 1: Codex analysis
        try:
            analysis = await self._analyze_with_codex(user_goal, workspace)
            result.codex_analysis = analysis
            result.steps.append(OrchestrationStepResult(
                step="analyze", agent="codex", summary=analysis[:200],
            ))
        except Exception as exc:
            result.status = "failed"
            result.verification_summary = f"Codex analysis failed: {exc}"
            return result

        # Check if Codex says no implementation needed
        if _is_reply_only_request(user_goal):
            result.status = "passed"
            result.verification_summary = analysis
            return result

        if _analysis_says_no_implementation_needed(analysis):
            result.status = "passed"
            result.verification_summary = "Codex determined no implementation needed."
            return result

        # Step 2-3: Implementation + verification loop
        for round_num in range(1, self._max_verify_rounds + 1):
            result.verify_round = round_num

            # Claude implementation
            try:
                impl = await self._implement_with_claude(
                    user_goal, analysis, workspace
                )
                result.claude_implementation = impl
                result.steps.append(OrchestrationStepResult(
                    step=f"implement_round_{round_num}", agent="claude", summary=impl[:200],
                ))
            except Exception as exc:
                result.status = "failed"
                result.verification_summary = f"Claude implementation failed: {exc}"
                return result

            # Codex verification
            try:
                verify_result = await self._verify_with_codex(
                    user_goal, analysis, impl, workspace,
                    pending_user_context=self._pending_user_context,
                )
                result.verification_summary = verify_result
                result.steps.append(OrchestrationStepResult(
                    step=f"verify_round_{round_num}", agent="codex",
                    summary=verify_result[:200],
                ))
            except Exception as exc:
                result.status = "failed"
                result.verification_summary = f"Codex verification failed: {exc}"
                return result

            decision = VerificationDecision.parse(verify_result)
            verification_drift = _detect_verification_delivery_drift(verify_result)

            # Codex verification must not bypass platform delivery either.
            if decision.decision == "pass" and verification_drift:
                decision = VerificationDecision(
                    decision="retry",
                    summary=decision.summary,
                    required_fix=(
                        "Codex 验收文本中检测到直接 Telegram delivery / token access: "
                        + "; ".join(verification_drift)
                    ),
                )

            # Force retry if Claude drift found, regardless of Codex decision.
            if decision.decision == "pass" and self._last_claude_drift_findings:
                decision = VerificationDecision(
                    decision="retry",
                    summary=decision.summary,
                    required_fix=(
                        "Claude 实施文本中检测到直接 Telegram delivery / token access: "
                        + "; ".join(self._last_claude_drift_findings)
                    ),
                )

            if decision.decision == "pass":
                result.status = "passed"
                return result
            elif decision.decision == "stop":
                result.status = "failed"
                result.verification_summary = decision.summary
                return result
            elif decision.decision == "need_user":
                result.status = "needs_user"
                result.verification_summary = decision.summary
                return result
            elif decision.decision == "retry":
                if round_num >= self._max_verify_rounds:
                    result.status = "failed"
                    result.verification_summary = (
                        f"Max verification rounds ({self._max_verify_rounds}) reached. "
                        f"Last verification: {decision.required_fix[:200]}"
                    )
                    return result
                analysis = f"Previous verification failed: {decision.required_fix}\n\nOriginal analysis: {analysis}"
                continue

        result.status = "needs_user"
        result.verification_summary = (
            f"Max verification rounds ({self._max_verify_rounds}) reached. "
            "Please review the changes and provide guidance."
        )
        return result

    async def _analyze_with_codex(self, goal: str, workspace: str) -> str:
        packet = build_codex_analysis_packet(
            user_goal=goal,
            workspace=workspace,
            budget=self._budget,
        )
        return await self._call_codex(
            packet.render(),
            workspace,
            interaction_mode="analysis",
        )

    async def _implement_with_claude(
        self, goal: str, analysis: str, workspace: str
    ) -> str:
        packet = build_claude_handoff_packet(
            user_goal=goal,
            codex_analysis=analysis,
            workspace=workspace,
            budget=self._budget,
        )
        return await self._call_claude(packet.render(), workspace)

    async def _verify_with_codex(
        self, goal: str, analysis: str, impl: str, workspace: str,
        *, pending_user_context: str = "",
    ) -> str:
        # Collect real workspace evidence — changed files, diff, test status
        changed_files, diff_summary, test_results = _collect_workspace_evidence(workspace)

        # Detect Claude direct-delivery drift before building the packet.
        claude_drift = _detect_claude_direct_delivery_drift(impl)
        self._last_claude_drift_findings = claude_drift
        claude_summary = impl
        if claude_drift:
            claude_summary = (
                f"WARNING: Claude 实施文本中检测到直接 Telegram delivery "
                f"/ token access 漂移:\n"
                + "\n".join(f"  - {f}" for f in claude_drift)
                + f"\n\n---原始 Claude 摘要---\n{impl[:2000]}"
            )

        packet = build_codex_verification_packet(
            user_goal=goal,
            codex_plan_summary=analysis,
            claude_completion_summary=claude_summary,
            changed_files=changed_files,
            diff_summary=diff_summary,
            test_results=test_results,
            workspace=workspace,
            budget=self._budget,
            pending_user_context=pending_user_context,
        )
        return await self._call_codex(
            packet.render(),
            workspace,
            interaction_mode="verification",
        )

    async def _call_codex(
        self,
        prompt: str,
        workspace: str,
        *,
        interaction_mode: str = "general",
    ) -> str:
        before_snapshot = (
            _capture_workspace_snapshot(workspace)
            if interaction_mode in ("analysis", "verification")
            else None
        )
        backend = self._codex
        result: str
        # Prefer real send_codex_prompt interface
        if hasattr(backend, "send_codex_prompt"):
            send_codex_prompt = backend.send_codex_prompt
            if _accepts_keyword(send_codex_prompt, "interaction_mode"):
                result = await send_codex_prompt(
                    workspace,
                    prompt,
                    interaction_mode=interaction_mode,
                )
            else:
                result = await send_codex_prompt(workspace, prompt)
        # Legacy test compatibility (echo/fake_response ignore workspace)
        elif hasattr(backend, "echo"):
            result = backend.echo(prompt)
        elif hasattr(backend, "fake_response"):
            result = backend.fake_response(prompt)
        else:
            raise NotImplementedError(
                "Codex backend must implement send_codex_prompt(workspace, prompt) -> str"
            )

        forbidden = _codex_forbidden_workspace_changes(
            before_snapshot,
            _capture_workspace_snapshot(workspace),
        )
        if forbidden:
            preview = ", ".join(forbidden[:8])
            if len(forbidden) > 8:
                preview = f"{preview}, +{len(forbidden) - 8} more"
            raise RuntimeError(
                "Codex 总工程师轮修改了实现文件，已停止闭环："
                f"{preview}。请把这些代码/测试/配置改动交给 Claude 执行。"
            )
        return result

    async def _call_claude(self, prompt: str, workspace: str) -> str:
        backend = self._claude
        # Prefer real AgentBackend.send interface
        if hasattr(backend, "send"):
            from wlcodex.agent_backend import AgentRequest
            result = await backend.send(AgentRequest(
                prompt=prompt,
                workspace_path=workspace,
            ))
            return result.text
        # Legacy test compatibility (echo/fake_response ignore workspace)
        if hasattr(backend, "echo"):
            return backend.echo(prompt)
        if hasattr(backend, "fake_response"):
            return backend.fake_response(prompt)
        raise NotImplementedError(
            "Claude backend must implement send(AgentRequest) -> AgentResult"
        )

    async def _call_claude_streaming(self, prompt: str, workspace: str):
        """Streaming variant: yields AgentStreamEvent objects.

        Falls back to blocking send() when send_streaming is not available.
        """
        from wlcodex.agent_backend import AgentRequest, AgentStreamEvent

        backend = self._claude
        if hasattr(backend, "send_streaming"):
            async for stream_event in backend.send_streaming(AgentRequest(
                prompt=prompt,
                workspace_path=workspace,
            )):
                yield stream_event
            return
        # Fallback: blocking send
        text = await self._call_claude(prompt, workspace)
        yield AgentStreamEvent(delta=text, event_type="text")

    async def run_streaming(
        self,
        user_goal: str,
        conversation_context: dict[str, Any] | None = None,
    ):
        """Streaming variant of run(): yields OrchestrationProgress at each stage.

        The caller consumes progress events and forwards them to the
        interaction renderer.  This is the same orchestration logic as
        run() but emits live progress instead of waiting until completion.
        """
        ctx = conversation_context or {}
        workspace = ctx.get("workspace", "wlcodex")
        result = OrchestrationResult(status="running")

        # Phase 1: Codex analysis
        yield OrchestrationProgress(
            phase=OrchestrationProgress.ANALYSIS_STARTED,
            text="Codex 正在分析需求...",
            agent="codex",
        )
        try:
            analysis = await self._analyze_with_codex(user_goal, workspace)
            result.codex_analysis = analysis
            result.steps.append(OrchestrationStepResult(
                step="analyze", agent="codex", summary=analysis[:200],
            ))
        except Exception as exc:
            result.status = "failed"
            result.verification_summary = f"Codex analysis failed: {exc}"
            yield OrchestrationProgress(
                phase=OrchestrationProgress.FAILED,
                text=f"Codex 分析失败：{exc}",
                full_text=str(exc),
                agent="codex",
            )
            yield OrchestrationProgress(
                phase=OrchestrationProgress.COMPLETE,
                text="",
                agent="",
                result_status="failed",
            )
            return

        yield OrchestrationProgress(
            phase=OrchestrationProgress.ANALYSIS_COMPLETE,
            text=analysis[:200],
            full_text=analysis,
            agent="codex",
        )

        # Check if Codex says no implementation needed
        if _is_reply_only_request(user_goal):
            result.status = "passed"
            result.verification_summary = analysis
            yield OrchestrationProgress(
                phase=OrchestrationProgress.COMPLETE,
                text="",
                full_text=analysis,
                agent="codex",
                result_status="passed",
            )
            return

        if _analysis_says_no_implementation_needed(analysis):
            result.status = "passed"
            result.verification_summary = "Codex determined no implementation needed."
            yield OrchestrationProgress(
                phase=OrchestrationProgress.COMPLETE,
                text="Codex 认为无需实施代码修改。",
                full_text=result.verification_summary,
                agent="codex",
                result_status="passed",
            )
            return

        # Phase 2-3: Implementation + verification loop
        for round_num in range(1, self._max_verify_rounds + 1):
            result.verify_round = round_num

            # Claude implementation (streaming)
            try:
                impl_accumulated = ""
                # Build the Claude handoff packet (same as _implement_with_claude)
                claude_packet = build_claude_handoff_packet(
                    user_goal=user_goal,
                    codex_analysis=analysis,
                    workspace=workspace,
                    budget=self._budget,
                )
                async for delta in self._call_claude_streaming(
                    claude_packet.render(), workspace
                ):
                    if delta.event_type == "error":
                        error_text = delta.delta or "Claude streaming returned error"
                        result.status = "failed"
                        result.verification_summary = (
                            f"Claude implementation failed: {error_text}"
                        )
                        yield OrchestrationProgress(
                            phase=OrchestrationProgress.FAILED,
                            text=f"Claude 实施失败：{error_text}",
                            full_text=error_text,
                            agent="claude",
                            round_num=round_num,
                        )
                        yield OrchestrationProgress(
                            phase=OrchestrationProgress.COMPLETE,
                            text="",
                            agent="",
                            result_status="failed",
                            round_num=round_num,
                        )
                        return
                    impl_accumulated += delta.delta
                    yield OrchestrationProgress(
                        phase=OrchestrationProgress.IMPL_DELTA,
                        text=delta.delta,
                        agent="claude",
                        round_num=round_num,
                    )
                result.claude_implementation = impl_accumulated
                result.steps.append(OrchestrationStepResult(
                    step=f"implement_round_{round_num}", agent="claude",
                    summary=impl_accumulated[:200],
                ))
            except Exception as exc:
                result.status = "failed"
                result.verification_summary = f"Claude implementation failed: {exc}"
                yield OrchestrationProgress(
                    phase=OrchestrationProgress.FAILED,
                    text=f"Claude 实施失败：{exc}",
                    full_text=str(exc),
                    agent="claude",
                    round_num=round_num,
                )
                yield OrchestrationProgress(
                    phase=OrchestrationProgress.COMPLETE,
                    text="",
                    agent="",
                    result_status="failed",
                    round_num=round_num,
                )
                return

            yield OrchestrationProgress(
                phase=OrchestrationProgress.IMPL_COMPLETE,
                text=impl_accumulated[:200],
                full_text=impl_accumulated,
                agent="claude",
                round_num=round_num,
            )

            # Codex verification
            yield OrchestrationProgress(
                phase=OrchestrationProgress.VERIFY_STARTED,
                text="Codex 正在验收...",
                agent="codex",
                round_num=round_num,
            )
            try:
                verify_result = await self._verify_with_codex(
                    user_goal, analysis, impl_accumulated, workspace,
                    pending_user_context=self._pending_user_context,
                )
                result.verification_summary = verify_result
                result.steps.append(OrchestrationStepResult(
                    step=f"verify_round_{round_num}", agent="codex",
                    summary=verify_result[:200],
                ))
            except Exception as exc:
                result.status = "failed"
                result.verification_summary = f"Codex verification failed: {exc}"
                yield OrchestrationProgress(
                    phase=OrchestrationProgress.FAILED,
                    text=f"Codex 验收失败：{exc}",
                    full_text=str(exc),
                    agent="codex",
                    round_num=round_num,
                )
                yield OrchestrationProgress(
                    phase=OrchestrationProgress.COMPLETE,
                    text="",
                    agent="",
                    result_status="failed",
                    round_num=round_num,
                )
                return

            yield OrchestrationProgress(
                phase=OrchestrationProgress.VERIFY_COMPLETE,
                text=verify_result[:200],
                full_text=verify_result,
                agent="codex",
                round_num=round_num,
            )

            decision = VerificationDecision.parse(verify_result)
            verification_drift = _detect_verification_delivery_drift(verify_result)

            # Codex verification must not bypass platform delivery either.
            if decision.decision == "pass" and verification_drift:
                decision = VerificationDecision(
                    decision="retry",
                    summary=decision.summary,
                    required_fix=(
                        "Codex 验收文本中检测到直接 Telegram delivery / token access: "
                        + "; ".join(verification_drift)
                    ),
                )

            # Force retry if Claude drift found, regardless of Codex decision.
            if decision.decision == "pass" and self._last_claude_drift_findings:
                decision = VerificationDecision(
                    decision="retry",
                    summary=decision.summary,
                    required_fix=(
                        "Claude 实施文本中检测到直接 Telegram delivery / token access: "
                        + "; ".join(self._last_claude_drift_findings)
                    ),
                )

            if decision.decision == "pass":
                result.status = "passed"
                yield OrchestrationProgress(
                    phase=OrchestrationProgress.COMPLETE,
                    text="验收通过",
                    full_text=verify_result,
                    agent="codex",
                    result_status="passed",
                    round_num=round_num,
                )
                return
            elif decision.decision == "stop":
                result.status = "failed"
                result.verification_summary = decision.summary
                yield OrchestrationProgress(
                    phase=OrchestrationProgress.COMPLETE,
                    text=f"验收停止：{decision.summary[:200]}",
                    full_text=decision.summary,
                    agent="codex",
                    result_status="failed",
                    round_num=round_num,
                )
                return
            elif decision.decision == "need_user":
                result.status = "needs_user"
                result.verification_summary = decision.summary
                yield OrchestrationProgress(
                    phase=OrchestrationProgress.COMPLETE,
                    text=f"需要用户判断：{decision.summary[:200]}",
                    full_text=decision.summary,
                    agent="codex",
                    result_status="needs_user",
                    round_num=round_num,
                )
                return
            elif decision.decision == "retry":
                if round_num >= self._max_verify_rounds:
                    result.status = "failed"
                    result.verification_summary = (
                        f"Max verification rounds ({self._max_verify_rounds}) reached. "
                        f"Last verification: {decision.required_fix[:200]}"
                    )
                    yield OrchestrationProgress(
                        phase=OrchestrationProgress.FAILED,
                        text=f"已达最大验收轮次（{self._max_verify_rounds}），最终验收仍要求返工。",
                        full_text=result.verification_summary,
                        agent="codex",
                        round_num=round_num,
                    )
                    yield OrchestrationProgress(
                        phase=OrchestrationProgress.COMPLETE,
                        text="",
                        agent="codex",
                        result_status="failed",
                        round_num=round_num,
                    )
                    return
                analysis = f"Previous verification failed: {decision.required_fix}\n\nOriginal analysis: {analysis}"
                continue

        result.status = "needs_user"
        result.verification_summary = (
            f"Max verification rounds ({self._max_verify_rounds}) reached. "
            "Please review the changes and provide guidance."
        )
        yield OrchestrationProgress(
            phase=OrchestrationProgress.COMPLETE,
            text=result.verification_summary[:200],
            full_text=result.verification_summary,
            agent="codex",
            result_status="needs_user",
            round_num=self._max_verify_rounds,
        )
