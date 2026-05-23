"""Staged-auto workflow helpers — stage constants, callback actions,
button builders, and run lookup predicates.

The staged-auto workflow replaces the old eager /auto pipeline with a
Codex-led, user-gated workflow that matches the user's local Codex-brain /
Claude-executor loop.

Safety invariants:
- Claude never starts from /auto without a user click.
- Codex never writes code in analysis or verification stages.
- Plain text in collecting_context is context, not execution permission.
- Trigger words do not grant execution permission.
"""

from __future__ import annotations

# --- Stage constants ---

AUTO_COLLECTING_CONTEXT = "collecting_context"
AUTO_DRAFT_READY = "draft_ready"
AUTO_CLAUDE_RUNNING = "claude_running"
AUTO_CLAUDE_DONE = "claude_done"
AUTO_VERIFYING = "verifying"
AUTO_RETRY_READY = "retry_ready"
AUTO_CODEX_TAKEOVER_RUNNING = "codex_takeover_running"
AUTO_COMPLETED = "completed"

AUTO_STAGE_STEPS = {
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
    AUTO_DRAFT_READY,
    AUTO_CLAUDE_DONE,
    AUTO_RETRY_READY,
}

# --- Callback actions ---

AUTO_FINAL_PLAN = "auto_final_plan"
AUTO_SHOW_DRAFT = "auto_show_draft"
AUTO_CANCEL = "auto_cancel"
AUTO_SEND_TO_CLAUDE = "auto_send_to_claude"
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

# --- Agent run roles ---

ROLE_AUTO_ANALYSIS = "auto_analysis"
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
        AUTO_COLLECTING_CONTEXT: "Codex 分析中（收集上下文）",
        AUTO_DRAFT_READY: "方案已就绪，等待用户决定",
        AUTO_CLAUDE_RUNNING: "Claude 执行中",
        AUTO_CLAUDE_DONE: "Claude 完成，等待验收",
        AUTO_VERIFYING: "Codex 验收中",
        AUTO_RETRY_READY: "验收失败，等待用户决定",
        AUTO_CODEX_TAKEOVER_RUNNING: "Codex 接管修复中",
        AUTO_COMPLETED: "任务完成",
    }
    return labels.get(step, step)


def build_auto_stage_buttons(
    conversation_id: int,
    stage: str,
    *,
    last_codex_analysis: str = "",
) -> list[list[dict[str, str]]]:
    """Build inline buttons for the given auto stage.

    Each button text must clearly describe the action it triggers.
    No vague labels like "继续" are used.

    When last_codex_analysis indicates needs_implementation: false,
    Claude execution buttons are suppressed and the user is directed
    to close the task or continue asking Codex.
    """
    def button(text: str, action: str) -> dict[str, str]:
        return {"text": text, "callback_data": f"conv:{conversation_id}:{action}"}

    has_visible_plan = bool(last_codex_analysis.strip())
    no_impl = (
        "needs_implementation: false" in last_codex_analysis.lower()
        if last_codex_analysis else False
    )

    if stage == AUTO_COLLECTING_CONTEXT:
        return [[
            button("生成最终方案", AUTO_FINAL_PLAN),
            button("查看当前草稿", AUTO_SHOW_DRAFT),
        ], [
            button("取消", AUTO_CANCEL),
        ]]

    if stage == AUTO_DRAFT_READY:
        if not has_visible_plan:
            return [[
                button("重写方案", AUTO_REWRITE_PLAN),
                button("继续补充", AUTO_CONTINUE_CONTEXT),
            ], [
                button("结束任务", AUTO_CLOSE),
            ]]
        if no_impl:
            # Codex determined no implementation is needed — suppress Claude gate.
            return [[
                button("继续问 Codex", AUTO_CONTINUE_CONTEXT),
                button("结束任务", AUTO_CLOSE),
            ], [
                button("查看当前草稿", AUTO_SHOW_DRAFT),
            ]]
        return [[
            button("交给 Claude 执行", AUTO_SEND_TO_CLAUDE),
            button("继续补充", AUTO_CONTINUE_CONTEXT),
        ], [
            button("重写方案", AUTO_REWRITE_PLAN),
            button("查看当前草稿", AUTO_SHOW_DRAFT),
        ], [
            button("Codex 接管修", AUTO_CODEX_TAKEOVER),
            button("结束任务", AUTO_CLOSE),
        ]]

    if stage == AUTO_CLAUDE_RUNNING:
        return [[
            button("查看状态", AUTO_VIEW_STATUS),
            button("打断 Claude", AUTO_INTERRUPT_CLAUDE),
        ]]

    if stage == AUTO_CLAUDE_DONE:
        return [[
            button("Codex 验收", AUTO_CODEX_VERIFY),
            button("查看 diff", AUTO_VIEW_DIFF),
        ], [
            button("发给 Claude 返工", AUTO_SEND_REPAIR_TO_CLAUDE),
            button("Codex 接管修", AUTO_CODEX_TAKEOVER),
        ], [
            button("结束任务", AUTO_CLOSE),
        ]]

    if stage == AUTO_VERIFYING:
        return [[
            button("查看状态", AUTO_VIEW_STATUS),
        ]]

    if stage == AUTO_RETRY_READY:
        return [[
            button("发给 Claude 返工", AUTO_SEND_REPAIR_TO_CLAUDE),
            button("继续补充", AUTO_CONTINUE_CONTEXT),
        ], [
            button("重写返工提示词", AUTO_REWRITE_REPAIR),
            button("Codex 接管修", AUTO_CODEX_TAKEOVER),
        ], [
            button("结束任务", AUTO_CLOSE),
        ]]

    # Default: status view for unknown or completed stages.
    return [[button("查看状态", AUTO_VIEW_STATUS)]]
