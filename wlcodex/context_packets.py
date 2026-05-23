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
        analysis: str = ""

    handoff_from_codex: HandoffFromCodex = field(default_factory=HandoffFromCodex)

    def render(self) -> str:
        base = super().render()
        h = self.handoff_from_codex
        lines: list[str] = [base, "", "handoff_from_codex:"]
        if h.objective:
            lines.append(f"  objective: {h.objective}")
        if h.analysis:
            lines.append(f"  analysis: {h.analysis}")
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
    handoff: bool = True,
) -> CodexAnalysisPacket:
    bgt = budget or ContextBudget()
    if handoff:
        analysis_only_constraints = [
            "Codex 本轮是总工程师分析/方案/交接，不要直接完成 Claude 的实现补丁。",
            "可以调用 skill、GitNexus、只读上下文检索和必要的方案验证工具。",
            "可以生成或写入 docs/ 或 .wlcodex/ 下的设计、评审、部署、验收类文档。",
            "不要修改业务代码、测试代码、依赖锁或配置，不要进入改代码/跑实现测试闭环。",
            "把实现交给 Claude，输出交接包：目标、文件、步骤、验收标准和禁止事项。",
        ]
        requested_output = (
            "Chief-engineer Claude handoff packet: root cause, files_to_touch, "
            "implementation_steps, acceptance_criteria, verification_plan, "
            "prohibited_changes. Do not implement code changes yourself."
        )
    else:
        analysis_only_constraints = [
            "本轮是 Codex 分析/查询，不是 Claude 交接。",
            "真实执行必要的查询和核验，不要只输出执行计划。",
            "可以按用户目标使用本地命令、GitNexus、ssh/curl/systemctl/journalctl/"
            "git log/docker ps 等方式确认事实。",
            "不要输出 Claude 交接包，不要把工作交给 Claude；直接回答结论、依据、"
            "风险和建议下一步。",
            "如果用户明确要求修改、部署、清理或跑实现闭环，可以按 Codex 当前能力执行；"
            "不确定时先说明风险并等待确认。",
        ]
        requested_output = (
            "中文可读结论：已执行的核验、关键依据、风险等级和建议下一步。"
        )
    return CodexAnalysisPacket(
        mode="chief_engineer",
        workspace=workspace,
        user_goal=user_goal,
        conversation_summary=trim_to_budget(conversation_summary, bgt.conversation_summary_tokens),
        relevant_files=relevant_files or [],
        recent_user_constraints=analysis_only_constraints + (constraints or []),
        token_budget=bgt.codex_analysis_tokens,
        requested_output=requested_output,
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
        "完整闭环：从分析到实现到验证，确保每个步骤闭环完成，不留半成品。",
        "没有漂移：严格遵守 Codex 分析方案和交接要求，不自行扩大或偏离范围。",
        "编码前思考：修改每处代码前先理解上下文和影响范围，谋定而后动。",
        "简洁优先：用最少代码完成目标，不引入不必要的抽象、重构或多余修改。",
        "精准修改：每个编辑只改需要改的地方，精确匹配，不残留调试代码或临时方案。",
        "目标驱动执行：始终以用户目标和验收标准为唯一方向，完成即止，不画蛇添足。",
    ]
    delivery_isolation_constraints = [
        "你是实施工程师，不是平台 reply agent。绝对不要发送 Telegram 消息。",
        "不要读取 WLCODEX_TELEGRAM_BOT_TOKEN 或任何 TELEGRAM_BOT_TOKEN 变量。",
        "不要调用 Telegram Bot API：不要用 sendMessage、editMessageText、sendChatAction 等。",
        "不要用 curl、httpie、requests、fetch 调用 api.telegram.org。",
        "不要声称已发送 Telegram 或输出 message_id=xxx。你无法发 Telegram。",
        "完成实施后只返回实施摘要给 orchestrator。最终回复由平台发送。",
    ]
    merged_constraints = claude_runtime_constraints + delivery_isolation_constraints + (constraints or [])
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
            analysis=trim_to_budget(codex_analysis, bgt.codex_to_claude_tokens),
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
    *,
    pending_user_context: str = "",
) -> CodexVerificationPacket:
    bgt = budget or ContextBudget()
    verification_constraints = [
        "你是验收 agent，不是平台 reply agent。绝对不要发送 Telegram 消息。",
        "不要读取 WLCODEX_TELEGRAM_BOT_TOKEN 或任何 token/env 变量。",
        "不要调用 Telegram Bot API：sendMessage、editMessageText、curl api.telegram.org 等。",
        "不要发出任何审批申请来获取 Telegram 发送权限。",
        "验收职责只读：检查 diff、跑测试、git diff --check、GitNexus detect_changes。",
        "验收结论只能是 decision: pass / retry / stop / need_user。",
        "发现 Claude 声称已发送 Telegram、直接调了 Telegram API、或输出 message_id=xxx，"
        "应判定为违规漂移并标记 retry 或 stop。",
        "最终用户回复由平台 controller 在 verification pass 后发送。",
    ]
    # Inject pending user context so Codex can consider mid-implementation follow-ups.
    conversation_context = ""
    if pending_user_context:
        conversation_context = (
            f"[用户在实施/验收阶段补充了以下上下文，请在验证时纳入考量]\n"
            f"{pending_user_context[:500]}"
        )
    return CodexVerificationPacket(
        mode="chief_engineer",
        workspace=workspace,
        user_goal=user_goal,
        conversation_summary=trim_to_budget(conversation_context, bgt.conversation_summary_tokens),
        relevant_files=changed_files or [],
        recent_user_constraints=verification_constraints,
        token_budget=bgt.claude_to_codex_tokens,
        original_goal=user_goal,
        codex_plan_summary=trim_to_budget(codex_plan_summary, bgt.conversation_summary_tokens),
        claude_completion_summary=trim_to_budget(claude_completion_summary, bgt.conversation_summary_tokens),
        changed_files=changed_files or [],
        diff_excerpt_or_summary=diff_summary,
        test_results=test_results,
        verification_question="Does the implementation meet all acceptance criteria?",
    )


def build_auto_context_packet(
    user_goal: str,
    conversation_summary: str = "",
    workspace: str = "wlcodex",
    budget: ContextBudget | None = None,
) -> CodexAnalysisPacket:
    """Build a read-only context collection packet for the /auto
    collecting_context stage.

    This packet is strictly read-only: Codex must not write files, start
    Claude, or output handoff packets. It should only analyze and wait for
    user context supplement.
    """
    bgt = budget or ContextBudget()
    return CodexAnalysisPacket(
        mode="auto_collecting_context",
        workspace=workspace,
        user_goal=user_goal,
        conversation_summary=trim_to_budget(conversation_summary, bgt.conversation_summary_tokens),
        recent_user_constraints=[
            "本轮是 /auto 的 Codex 上下文收集阶段。",
            "真实执行必要的查询和远程核验，不要只输出核验计划。",
            "允许使用 ssh/curl/systemctl/journalctl/git log/docker ps 等命令确认事实。",
            "不要因为任务是查询/核验就停止在方案层；能查就直接查，给出证据和结论。",
            "不要启动 Claude，不要输出最终执行包。",
            "如果信息不足，说明还缺什么；如果已有判断，给出阶段性结论。",
        ],
        token_budget=bgt.codex_analysis_tokens,
        requested_output="中文阶段性结果：已执行的核验、证据、当前判断、缺失信息。",
    )


def build_auto_final_plan_packet(
    user_goal: str,
    conversation_summary: str = "",
    workspace: str = "wlcodex",
    budget: ContextBudget | None = None,
) -> CodexAnalysisPacket:
    """Build a final plan packet for the auto draft_ready stage.

    Codex produces: diagnosis, confidence, files, Claude execution prompt,
    acceptance criteria, prohibited changes, and verification plan.
    """
    bgt = budget or ContextBudget()
    return CodexAnalysisPacket(
        mode="auto_final_plan",
        workspace=workspace,
        user_goal=user_goal,
        conversation_summary=trim_to_budget(conversation_summary, bgt.conversation_summary_tokens),
        recent_user_constraints=[
            "输出 /auto 的最终方案或最终结论。",
            "查询/核验类任务必须基于已执行证据给最终结论，不要只输出下一步计划。",
            "如果需要实现，再包含给 Claude 的执行提示词。",
            "如果无需实现，明确写 needs_implementation: false，并说明不要交给 Claude。",
            "保留用户补充的约束和禁止事项。",
            "如果涉及 LightFeeV2 线上排障/状态检查，必须先运行 python scripts/diagnose_live.py --json",
            "并将输出的 diagnose JSON 完整嵌入 ```json 代码块，作为 evidence manifest。",
        ],
        token_budget=bgt.codex_analysis_tokens,
        requested_output=(
            "中文最终结论/方案，包含 diagnosis, evidence, confidence, files_to_touch, "
            "claude_prompt（仅实现类任务需要）, acceptance_criteria, verification_result。"
            "如有 diagnose JSON，必须用 ```json 代码块完整附上。"
        ),
    )


def build_auto_verification_packet(
    user_goal: str,
    codex_plan_summary: str = "",
    claude_completion_summary: str = "",
    changed_files: list[str] | None = None,
    test_results: str = "",
    diff_summary: str = "",
    workspace: str = "wlcodex",
    budget: ContextBudget | None = None,
    *,
    verify_round: int = 1,
    pending_user_context: str = "",
) -> CodexVerificationPacket:
    """Build a verification packet for the auto verifying stage.

    Similar to build_codex_verification_packet but with auto-specific
    constraints and verify_round tracking.
    """
    bgt = budget or ContextBudget()
    verification_constraints = [
        "你是 /auto 工作流的验收 agent。",
        "你是验收 agent，不是平台 reply agent。绝对不要发送 Telegram 消息。",
        "不要读取 WLCODEX_TELEGRAM_BOT_TOKEN 或任何 token/env 变量。",
        "不要调用 Telegram Bot API：sendMessage、editMessageText、curl api.telegram.org 等。",
        "不要发出任何审批申请来获取 Telegram 发送权限。",
        "验收职责只读：检查 diff、跑测试、git diff --check、GitNexus detect_changes。",
        f"这是第 {verify_round} 轮验收。",
        "验收结论只能是 decision: pass / retry / stop / need_user。",
        "如果判定 retry，必须输出具体的给 Claude 的返工提示词（repair_prompt）。",
        "发现 Claude 声称已发送 Telegram、直接调了 Telegram API、或输出 message_id=xxx，"
        "应判定为违规漂移并标记 retry 或 stop。",
        "最终用户回复由平台 controller 在 verification pass 后发送。",
    ]
    conversation_context = ""
    if pending_user_context:
        conversation_context = (
            f"[用户在验收阶段补充了以下上下文]\n"
            f"{pending_user_context[:500]}"
        )
    return CodexVerificationPacket(
        mode="auto_verification",
        workspace=workspace,
        user_goal=user_goal,
        conversation_summary=trim_to_budget(conversation_context, bgt.conversation_summary_tokens),
        relevant_files=changed_files or [],
        recent_user_constraints=verification_constraints,
        token_budget=bgt.claude_to_codex_tokens,
        original_goal=user_goal,
        codex_plan_summary=trim_to_budget(codex_plan_summary, bgt.conversation_summary_tokens),
        claude_completion_summary=trim_to_budget(claude_completion_summary, bgt.conversation_summary_tokens),
        changed_files=changed_files or [],
        diff_excerpt_or_summary=diff_summary,
        test_results=test_results,
        verification_question="Does the implementation meet all acceptance criteria?",
    )


def build_auto_repair_packet(
    user_goal: str,
    codex_plan_summary: str = "",
    claude_completion_summary: str = "",
    verification_result: str = "",
    workspace: str = "wlcodex",
    budget: ContextBudget | None = None,
) -> CodexHandoffPacket:
    """Build a Claude repair packet for the auto retry_ready stage.

    This packet contains the focused repair prompt that Codex generated
    during verification failure, combined with the original goal and plan.
    """
    bgt = budget or ContextBudget()
    repair_constraints = [
        "你是修复工程师，不是平台 reply agent。绝对不要发送 Telegram 消息。",
        "不要读取 WLCODEX_TELEGRAM_BOT_TOKEN 或任何 TELEGRAM_BOT_TOKEN 变量。",
        "不要调用 Telegram Bot API。",
        "不要声称已发送 Telegram 或输出 message_id=xxx。你无法发 Telegram。",
        "这是验收失败后的返工，只修复验收指出的问题，不要扩大范围。",
        "严格遵守 Codex 方案和验收标准，不要偏离范围。",
    ]
    steps = []
    if verification_result:
        steps.append(f"验收失败原因：{verification_result[:500]}")
    if codex_plan_summary:
        steps.append(f"原始方案摘要：{codex_plan_summary[:300]}")
    return ClaudeHandoffPacket(
        mode="auto_repair",
        workspace=workspace,
        user_goal=user_goal,
        conversation_summary="",
        relevant_files=[],
        recent_user_constraints=repair_constraints,
        acceptance_criteria=[],
        token_budget=bgt.codex_to_claude_tokens,
        handoff_from_codex=ClaudeHandoffPacket.HandoffFromCodex(
            objective=f"修复验收失败的问题：{user_goal}",
            analysis=verification_result[:1000] if verification_result else "",
            steps=steps,
            acceptance_criteria=["验收指出的所有问题已修复", "原验收标准全部通过"],
            prohibited_changes=["不要引入新功能", "不要修改验收未涉及的文件"],
        ),
    )
