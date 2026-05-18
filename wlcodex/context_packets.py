"""Compact prompt packet builders with token-budget enforcement.

Never include raw Telegram transcripts in model prompts.
Each model call receives a compact packet built for that call.
"""

from __future__ import annotations

from dataclasses import dataclass, field


CHINESE_OUTPUT_POLICY = (
    "必须使用中文回复用户可见内容；不要输出英文，除非用户明确要求英文，"
    "或代码、命令、文件名、错误原文必须保留。"
)


def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def trim_to_budget(text: str, max_tokens: int) -> str:
    if approx_tokens(text) <= max_tokens:
        return text
    char_budget = max_tokens * 4
    return text[:char_budget]


@dataclass
class ContextBudget:
    codex_analysis_tokens: int = 2500
    codex_to_claude_tokens: int = 1500
    claude_to_codex_tokens: int = 2500
    conversation_summary_tokens: int = 800


@dataclass
class ContextPacket:
    mode: str
    workspace: str
    user_goal: str
    current_request: str = ""
    conversation_summary: str = ""
    relevant_files: list[str] = field(default_factory=list)
    recent_user_constraints: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    token_budget: int = 0
    output_language_policy: str = CHINESE_OUTPUT_POLICY

    def render(self) -> str:
        lines: list[str] = []
        if self.mode:
            lines.append(f"mode: {self.mode}")
        if self.workspace:
            lines.append(f"workspace: {self.workspace}")
        if self.user_goal:
            lines.append(f"user_goal: {self.user_goal}")
        if self.current_request:
            lines.append(f"current_request: {self.current_request}")
        if self.conversation_summary:
            lines.append(f"conversation_summary: {self.conversation_summary}")
        if self.relevant_files:
            lines.append(f"relevant_files: {', '.join(self.relevant_files)}")
        if self.recent_user_constraints:
            lines.append(f"recent_user_constraints: {'; '.join(self.recent_user_constraints)}")
        if self.acceptance_criteria:
            lines.append(f"acceptance_criteria: {'; '.join(self.acceptance_criteria)}")
        if self.output_language_policy:
            lines.append(f"output_language_policy: {self.output_language_policy}")
        if self.token_budget:
            lines.append(f"token_budget: {self.token_budget}")
        return "\n".join(lines)

    def summary(self) -> str:
        parts = [self.user_goal]
        if self.current_request:
            parts.append(self.current_request)
        if self.relevant_files:
            parts.append(f"files: {', '.join(self.relevant_files[:5])}")
        return " | ".join(parts)

    def within_budget(self) -> bool:
        if self.token_budget <= 0:
            return True
        return approx_tokens(self.render()) <= self.token_budget


@dataclass
class CodexAnalysisPacket(ContextPacket):
    requested_output: str = ""

    def render(self) -> str:
        base = super().render()
        if self.requested_output:
            base += f"\nrequested_output: {self.requested_output}"
        return base


@dataclass
class ClaudeHandoffPacket(ContextPacket):

    @dataclass
    class HandoffFromCodex:
        objective: str = ""
        files_to_touch: list[str] = field(default_factory=list)
        steps: list[str] = field(default_factory=list)
        constraints: list[str] = field(default_factory=list)
        acceptance_criteria: list[str] = field(default_factory=list)
        prohibited_changes: list[str] = field(default_factory=list)

    handoff_from_codex: HandoffFromCodex = field(default_factory=HandoffFromCodex)

    def render(self) -> str:
        base = super().render()
        h = self.handoff_from_codex
        lines: list[str] = [base, "", "handoff_from_codex:"]
        if h.objective:
            lines.append(f"  objective: {h.objective}")
        if h.files_to_touch:
            lines.append(f"  files_to_touch: {', '.join(h.files_to_touch)}")
        if h.steps:
            for i, step in enumerate(h.steps, 1):
                lines.append(f"  step_{i}: {step}")
        if h.constraints:
            lines.append(f"  constraints: {'; '.join(h.constraints)}")
        if h.acceptance_criteria:
            lines.append(f"  acceptance_criteria: {'; '.join(h.acceptance_criteria)}")
        if h.prohibited_changes:
            lines.append(f"  prohibited_changes: {'; '.join(h.prohibited_changes)}")
        return "\n".join(lines)


@dataclass
class CodexVerificationPacket(ContextPacket):
    original_goal: str = ""
    codex_plan_summary: str = ""
    claude_completion_summary: str = ""
    changed_files: list[str] = field(default_factory=list)
    diff_excerpt_or_summary: str = ""
    test_results: str = ""
    verification_question: str = ""

    def render(self) -> str:
        base = super().render()
        lines: list[str] = [base, "", "verification_context:"]
        if self.original_goal:
            lines.append(f"  original_goal: {self.original_goal}")
        if self.codex_plan_summary:
            lines.append(f"  codex_plan_summary: {self.codex_plan_summary}")
        if self.claude_completion_summary:
            lines.append(f"  claude_completion_summary: {self.claude_completion_summary}")
        if self.changed_files:
            lines.append(f"  changed_files: {', '.join(self.changed_files)}")
        if self.diff_excerpt_or_summary:
            lines.append(f"  diff_excerpt_or_summary: {self.diff_excerpt_or_summary}")
        if self.test_results:
            lines.append(f"  test_results: {self.test_results}")
        if self.verification_question:
            lines.append(f"  verification_question: {self.verification_question}")
        return "\n".join(lines)


def build_codex_analysis_packet(
    user_goal: str,
    conversation_summary: str = "",
    relevant_files: list[str] | None = None,
    constraints: list[str] | None = None,
    workspace: str = "wlcodex",
    budget: ContextBudget | None = None,
) -> CodexAnalysisPacket:
    bgt = budget or ContextBudget()
    analysis_only_constraints = [
        "Codex 本轮是总工程师分析/方案/交接，不要直接完成 Claude 的实现补丁。",
        "可以调用 skill、GitNexus、只读上下文检索和必要的方案验证工具。",
        "可以生成或写入 docs/ 或 .wlcodex/ 下的设计、评审、部署、验收类文档。",
        "不要修改业务代码、测试代码、依赖锁或配置，不要进入改代码/跑实现测试闭环。",
        "把实现交给 Claude，输出交接包：目标、文件、步骤、验收标准和禁止事项。",
    ]
    return CodexAnalysisPacket(
        mode="chief_engineer",
        workspace=workspace,
        user_goal=user_goal,
        conversation_summary=trim_to_budget(conversation_summary, bgt.conversation_summary_tokens),
        relevant_files=relevant_files or [],
        recent_user_constraints=analysis_only_constraints + (constraints or []),
        token_budget=bgt.codex_analysis_tokens,
        requested_output=(
            "Chief-engineer Claude handoff packet: root cause, files_to_touch, "
            "implementation_steps, acceptance_criteria, verification_plan, "
            "prohibited_changes. Do not implement code changes yourself."
        ),
    )


def build_claude_handoff_packet(
    user_goal: str,
    codex_analysis: str,
    implementation_steps: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    files_to_touch: list[str] | None = None,
    constraints: list[str] | None = None,
    prohibited_changes: list[str] | None = None,
    telegram_transcript: str = "",
    workspace: str = "wlcodex",
    budget: ContextBudget | None = None,
) -> ClaudeHandoffPacket:
    bgt = budget or ContextBudget()
    # Telegram transcript must NOT be included in the packet
    steps = implementation_steps or []
    claude_runtime_constraints = [
        "你已经进入 Chief Engineer 工作流的实施阶段。Codex 已审批方案，不需再次请示计划审批。",
        "立即落地实施所有代码修改：修改文件、创建测试、运行命令。不要输出计划或问是否批准此计划。",
        "若实现遇到不可解决的障碍才输出阻塞原因，否则必须产生实际 diff/文件变更。",
        "长时间测试/构建/命令不要用一个前台 Bash 卡住；使用 run_in_background 并用 BashOutput 查询进度。",
        "执行过程中给出可流式输出的简短进度，避免长时间静默。",
    ]
    merged_constraints = claude_runtime_constraints + (constraints or [])
    return ClaudeHandoffPacket(
        mode="chief_engineer",
        workspace=workspace,
        user_goal=user_goal,
        conversation_summary="",
        relevant_files=files_to_touch or [],
        recent_user_constraints=merged_constraints,
        acceptance_criteria=acceptance_criteria or [],
        token_budget=bgt.codex_to_claude_tokens,
        handoff_from_codex=ClaudeHandoffPacket.HandoffFromCodex(
            objective=user_goal,
            files_to_touch=files_to_touch or [],
            steps=steps,
            constraints=merged_constraints,
            acceptance_criteria=acceptance_criteria or [],
            prohibited_changes=prohibited_changes or [],
        ),
    )


def build_codex_verification_packet(
    user_goal: str,
    codex_plan_summary: str = "",
    claude_completion_summary: str = "",
    changed_files: list[str] | None = None,
    test_results: str = "",
    diff_summary: str = "",
    workspace: str = "wlcodex",
    budget: ContextBudget | None = None,
) -> CodexVerificationPacket:
    bgt = budget or ContextBudget()
    return CodexVerificationPacket(
        mode="chief_engineer",
        workspace=workspace,
        user_goal=user_goal,
        conversation_summary="",
        relevant_files=changed_files or [],
        token_budget=bgt.claude_to_codex_tokens,
        original_goal=user_goal,
        codex_plan_summary=trim_to_budget(codex_plan_summary, bgt.conversation_summary_tokens),
        claude_completion_summary=trim_to_budget(claude_completion_summary, bgt.conversation_summary_tokens),
        changed_files=changed_files or [],
        diff_excerpt_or_summary=diff_summary,
        test_results=test_results,
        verification_question="Does the implementation meet all acceptance criteria?",
    )
