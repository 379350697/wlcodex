"""Staged-auto workflow helpers — stage constants, callback actions,
button builders, and run lookup predicates.

The staged-auto workflow is role-led: diagnosis/architecture, implementation,
black-box testing, independent audit, and completion or rework. Model/backend
names are implementation details behind those engineering roles.

Safety invariants:
- A developer never starts from /auto without a user click.
- Diagnosis, architecture, testing, and audit stages do not write product code.
- Plain text in collecting_context is context, not execution permission.
- Trigger words do not grant execution permission.
"""

from __future__ import annotations

# --- Stage constants ---

AUTO_COLLECTING_CONTEXT = "collecting_context"
AUTO_ROUTE_SELECT = "route_select"
AUTO_DRAFT_READY = "draft_ready"
AUTO_CLAUDE_RUNNING = "claude_running"
AUTO_CLAUDE_DONE = "claude_done"
AUTO_VERIFYING = "verifying"
AUTO_RETRY_READY = "retry_ready"
AUTO_CODEX_TAKEOVER_RUNNING = "codex_takeover_running"
AUTO_COMPLETED = "completed"

AUTO_STAGE_STEPS = {
    AUTO_ROUTE_SELECT,
    AUTO_COLLECTING_CONTEXT,
    AUTO_DRAFT_READY,
    AUTO_CLAUDE_RUNNING,
    AUTO_CLAUDE_DONE,
    AUTO_VERIFYING,
    AUTO_RETRY_READY,
    AUTO_CODEX_TAKEOVER_RUNNING,
    AUTO_COMPLETED,
}

# Stages where the orchestration run is actively running (not waiting for user).
AUTO_RUNNING_STAGES = {
    AUTO_COLLECTING_CONTEXT,
    AUTO_CLAUDE_RUNNING,
    AUTO_VERIFYING,
    AUTO_CODEX_TAKEOVER_RUNNING,
}

# Stages where the orchestration run is waiting for a user button click.
AUTO_WAITING_STAGES = {
    AUTO_ROUTE_SELECT,
    AUTO_DRAFT_READY,
    AUTO_CLAUDE_DONE,
    AUTO_RETRY_READY,
}

# --- Callback actions ---

AUTO_FINAL_PLAN = "auto_final_plan"
AUTO_ROUTE_DIAGNOSE = "auto_route_diagnose"
AUTO_ROUTE_DESIGN = "auto_route_design"
AUTO_ROUTE_CODEX_EXECUTE = "auto_route_codex_execute"
AUTO_ROUTE_CLAUDE_EXECUTE = "auto_route_claude_execute"
AUTO_SHOW_DRAFT = "auto_show_draft"
AUTO_CANCEL = "auto_cancel"
AUTO_SEND_TO_CLAUDE = "auto_send_to_claude"
AUTO_SEND_TO_CODEX = "auto_send_to_codex"
AUTO_CONTINUE_CONTEXT = "auto_continue_context"
AUTO_REWRITE_PLAN = "auto_rewrite_plan"
AUTO_CODEX_TAKEOVER = "auto_codex_takeover"
AUTO_CLOSE = "auto_close"
AUTO_CODEX_VERIFY = "auto_codex_verify"
AUTO_SEND_REPAIR_TO_CLAUDE = "auto_send_repair_to_claude"
AUTO_REWRITE_REPAIR = "auto_rewrite_repair"
AUTO_INTERRUPT_CLAUDE = "auto_interrupt_claude"
AUTO_VIEW_DIFF = "auto_view_diff"
AUTO_VIEW_STATUS = "auto_view_status"
TEAM_VIEW_STATUS = "team_view_status"
TEAM_VIEW_ARTIFACTS = "team_view_artifacts"

# --- Agent run roles ---

ROLE_AUTO_ANALYSIS = "auto_analysis"
ROLE_AUTO_CONTEXT_SUPPLEMENT = "auto_context_supplement"
ROLE_AUTO_FINAL_PLAN = "auto_final_plan"
ROLE_AUTO_VERIFICATION = "auto_verification"
ROLE_AUTO_IMPLEMENTATION = "auto_implementation"
ROLE_AUTO_REPAIR = "auto_repair"
ROLE_AUTO_CODEX_TAKEOVER = "auto_codex_takeover"


def is_active_auto_stage(run: object | None) -> bool:
    """Return True if the given orchestration run is in an active auto stage
    (running or waiting for user, not completed/aborted/passed/failed)."""
    if run is None:
        return False
    status = getattr(run, "status", "")
    step = getattr(run, "current_step", "")
    return (
        status in {"running", "needs_user"}
        and step in AUTO_STAGE_STEPS
        and step != AUTO_COMPLETED
    )


def is_auto_collecting_context(run: object | None) -> bool:
    """Return True if the run is in the collecting_context stage."""
    if run is None:
        return False
    return (
        getattr(run, "status", "") in {"running", "needs_user"}
        and getattr(run, "current_step", "") == AUTO_COLLECTING_CONTEXT
    )


def auto_stage_label(step: str) -> str:
    """Return a human-readable label for an auto stage step."""
    labels = {
        AUTO_COLLECTING_CONTEXT: "工程师正在分析中",
        AUTO_ROUTE_SELECT: "等待选择执行路线",
        AUTO_DRAFT_READY: "方案已就绪，等待用户决定",
        AUTO_CLAUDE_RUNNING: "开发工程师执行中",
        AUTO_CLAUDE_DONE: "开发完成，测试通过，等待验收",
        AUTO_VERIFYING: "审计工程师验收中",
        AUTO_RETRY_READY: "验收失败，等待用户决定",
        AUTO_CODEX_TAKEOVER_RUNNING: "GPT 开发工程师接管修复中",
        AUTO_COMPLETED: "任务完成",
    }
    return labels.get(step, step)


def build_auto_stage_buttons(
    conversation_id: int,
    stage: str,
    *,
    last_codex_analysis: str = "",
    codex_implementer_enabled: bool = False,
) -> list[list[dict[str, str]]]:
    """Build inline buttons for the given auto stage.

    Each button text must clearly describe the action it triggers.
    No vague labels like "继续" are used.

    Codex may suggest that no implementation is needed, but the user decides
    whether to hand off, continue with more context, take over, or close.
    """
    def button(text: str, action: str) -> dict[str, str]:
        return {"text": text, "callback_data": f"conv:{conversation_id}:{action}"}

    has_visible_plan = bool(last_codex_analysis.strip())
    if stage == AUTO_ROUTE_SELECT:
        buttons = [
            button("诊断", AUTO_ROUTE_DIAGNOSE),
            button("设计", AUTO_ROUTE_DESIGN),
            button("GPT 执行", AUTO_ROUTE_CODEX_EXECUTE),
        ]
        buttons.append(button("DeepSeek 执行", AUTO_ROUTE_CLAUDE_EXECUTE))
        return [
            buttons,
            [
                button("取消", AUTO_CANCEL),
            ],
        ]

    if stage == AUTO_COLLECTING_CONTEXT:
        return [[
            button("生成最终方案", AUTO_FINAL_PLAN),
            button("查看当前草稿", AUTO_SHOW_DRAFT),
        ], [
            button("团队状态", TEAM_VIEW_STATUS),
            button("团队证据", TEAM_VIEW_ARTIFACTS),
        ], [
            button("取消", AUTO_CANCEL),
        ]]

    if stage == AUTO_DRAFT_READY:
        if not has_visible_plan:
            return [[
                button("继续补充", AUTO_CONTINUE_CONTEXT),
                button("结束任务", AUTO_CLOSE),
            ], [
                button("团队状态", TEAM_VIEW_STATUS),
                button("团队证据", TEAM_VIEW_ARTIFACTS),
            ]]
        handoff_buttons = [button("交给 DeepSeek 开发工程师", AUTO_SEND_TO_CLAUDE)]
        if codex_implementer_enabled:
            handoff_buttons.append(button("交给 GPT 开发工程师", AUTO_SEND_TO_CODEX))
        return [
            handoff_buttons,
            [
                button("继续补充", AUTO_CONTINUE_CONTEXT),
                button("查看当前草稿", AUTO_SHOW_DRAFT),
            ],
            [
                button("团队状态", TEAM_VIEW_STATUS),
                button("团队证据", TEAM_VIEW_ARTIFACTS),
            ],
            [
                button("GPT 开发工程师接管", AUTO_CODEX_TAKEOVER),
                button("结束任务", AUTO_CLOSE),
            ],
        ]

    if stage == AUTO_CLAUDE_RUNNING:
        return [[
            button("查看状态", AUTO_VIEW_STATUS),
            button("打断执行", AUTO_INTERRUPT_CLAUDE),
        ], [
            button("团队状态", TEAM_VIEW_STATUS),
            button("团队证据", TEAM_VIEW_ARTIFACTS),
        ]]

    if stage == AUTO_CLAUDE_DONE:
        return [[
            button("审计工程师验收", AUTO_CODEX_VERIFY),
            button("查看 diff", AUTO_VIEW_DIFF),
        ], [
            button("DeepSeek 开发工程师返工", AUTO_SEND_REPAIR_TO_CLAUDE),
            button("GPT 开发工程师接管", AUTO_CODEX_TAKEOVER),
        ], [
            button("团队状态", TEAM_VIEW_STATUS),
            button("团队证据", TEAM_VIEW_ARTIFACTS),
        ], [
            button("结束任务", AUTO_CLOSE),
        ]]

    if stage == AUTO_VERIFYING:
        return [[
            button("查看状态", AUTO_VIEW_STATUS),
        ], [
            button("团队状态", TEAM_VIEW_STATUS),
            button("团队证据", TEAM_VIEW_ARTIFACTS),
        ]]

    if stage == AUTO_RETRY_READY:
        handoff_buttons = [button("DeepSeek 开发工程师返工", AUTO_SEND_REPAIR_TO_CLAUDE)]
        if codex_implementer_enabled:
            handoff_buttons.append(button("交给 GPT 开发工程师", AUTO_SEND_TO_CODEX))
        return [
            handoff_buttons,
            [
                button("继续补充", AUTO_CONTINUE_CONTEXT),
                button("重写返工提示词", AUTO_REWRITE_REPAIR),
            ],
            [
                button("团队状态", TEAM_VIEW_STATUS),
                button("团队证据", TEAM_VIEW_ARTIFACTS),
            ],
            [
                button("GPT 开发工程师接管", AUTO_CODEX_TAKEOVER),
                button("结束任务", AUTO_CLOSE),
            ],
        ]

    # Default: status view for unknown or completed stages.
    return [[
        button("查看状态", AUTO_VIEW_STATUS),
        button("团队状态", TEAM_VIEW_STATUS),
        button("团队证据", TEAM_VIEW_ARTIFACTS),
    ]]
