"""Tests for auto_workflow stage constants, predicates, and button sets."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from wlcodex.auto_workflow import (
    AUTO_CLAUDE_DONE,
    AUTO_CLAUDE_RUNNING,
    AUTO_COLLECTING_CONTEXT,
    AUTO_CODEX_TAKEOVER_RUNNING,
    AUTO_COMPLETED,
    AUTO_DRAFT_READY,
    AUTO_RETRY_READY,
    AUTO_VERIFYING,
    AUTO_FINAL_PLAN,
    AUTO_SEND_TO_CLAUDE,
    AUTO_CONTINUE_CONTEXT,
    AUTO_REWRITE_PLAN,
    AUTO_CODEX_TAKEOVER,
    AUTO_CLOSE,
    AUTO_CODEX_VERIFY,
    AUTO_SEND_REPAIR_TO_CLAUDE,
    AUTO_REWRITE_REPAIR,
    AUTO_CANCEL,
    AUTO_SHOW_DRAFT,
    AUTO_VIEW_STATUS,
    AUTO_INTERRUPT_CLAUDE,
    AUTO_VIEW_DIFF,
    AUTO_STAGE_STEPS,
    AUTO_RUNNING_STAGES,
    AUTO_WAITING_STAGES,
    ROLE_AUTO_ANALYSIS,
    ROLE_AUTO_FINAL_PLAN,
    ROLE_AUTO_VERIFICATION,
    ROLE_AUTO_IMPLEMENTATION,
    ROLE_AUTO_REPAIR,
    ROLE_AUTO_CODEX_TAKEOVER,
    auto_stage_label,
    build_auto_stage_buttons,
    is_active_auto_stage,
    is_auto_collecting_context,
)


def _labels(buttons: list[list[dict[str, str]]]) -> list[str]:
    return [button["text"] for row in buttons for button in row]


def _actions(buttons: list[list[dict[str, str]]]) -> list[str]:
    return [button["callback_data"].split(":")[-1] for row in buttons for button in row]


class TestStageConstants:
    def test_all_stage_steps_are_strings(self) -> None:
        for step in AUTO_STAGE_STEPS:
            assert isinstance(step, str)
            assert step == step.strip()

    def test_running_stages_are_subset(self) -> None:
        assert AUTO_RUNNING_STAGES.issubset(AUTO_STAGE_STEPS)

    def test_waiting_stages_are_subset(self) -> None:
        assert AUTO_WAITING_STAGES.issubset(AUTO_STAGE_STEPS)

    def test_running_and_waiting_are_disjoint(self) -> None:
        assert AUTO_RUNNING_STAGES.isdisjoint(AUTO_WAITING_STAGES)

    def test_completed_is_not_running_or_waiting(self) -> None:
        assert AUTO_COMPLETED not in AUTO_RUNNING_STAGES
        assert AUTO_COMPLETED not in AUTO_WAITING_STAGES


class TestCallbackActions:
    def test_all_actions_are_strings(self) -> None:
        actions = [
            AUTO_FINAL_PLAN, AUTO_SHOW_DRAFT, AUTO_CANCEL,
            AUTO_SEND_TO_CLAUDE, AUTO_CONTINUE_CONTEXT,
            AUTO_REWRITE_PLAN, AUTO_CODEX_TAKEOVER, AUTO_CLOSE,
            AUTO_CODEX_VERIFY, AUTO_SEND_REPAIR_TO_CLAUDE,
            AUTO_REWRITE_REPAIR, AUTO_INTERRUPT_CLAUDE,
            AUTO_VIEW_DIFF, AUTO_VIEW_STATUS,
        ]
        for action in actions:
            assert isinstance(action, str)
            assert action.startswith("auto_") or action in {"diff", "status"}

    def test_role_constants_are_unique(self) -> None:
        roles = [
            ROLE_AUTO_ANALYSIS, ROLE_AUTO_FINAL_PLAN,
            ROLE_AUTO_VERIFICATION, ROLE_AUTO_IMPLEMENTATION,
            ROLE_AUTO_REPAIR, ROLE_AUTO_CODEX_TAKEOVER,
        ]
        assert len(roles) == len(set(roles))


class TestIsActiveAutoStage:
    def test_none_returns_false(self) -> None:
        assert is_active_auto_stage(None) is False

    def test_running_collecting_context(self) -> None:
        run = SimpleNamespace(status="running", current_step=AUTO_COLLECTING_CONTEXT)
        assert is_active_auto_stage(run) is True

    def test_needs_user_draft_ready(self) -> None:
        run = SimpleNamespace(status="needs_user", current_step=AUTO_DRAFT_READY)
        assert is_active_auto_stage(run) is True

    def test_passed_completed_is_not_active(self) -> None:
        run = SimpleNamespace(status="passed", current_step=AUTO_COMPLETED)
        assert is_active_auto_stage(run) is False

    def test_failed_is_not_active(self) -> None:
        run = SimpleNamespace(status="failed", current_step=AUTO_COLLECTING_CONTEXT)
        assert is_active_auto_stage(run) is False

    def test_aborted_is_not_active(self) -> None:
        run = SimpleNamespace(status="aborted", current_step=AUTO_DRAFT_READY)
        assert is_active_auto_stage(run) is False

    def test_non_auto_step_is_not_active(self) -> None:
        run = SimpleNamespace(status="running", current_step="implementation")
        assert is_active_auto_stage(run) is False


class TestIsAutoCollectingContext:
    def test_collecting_context_running(self) -> None:
        run = SimpleNamespace(status="running", current_step=AUTO_COLLECTING_CONTEXT)
        assert is_auto_collecting_context(run) is True

    def test_collecting_context_needs_user(self) -> None:
        run = SimpleNamespace(status="needs_user", current_step=AUTO_COLLECTING_CONTEXT)
        assert is_auto_collecting_context(run) is True

    def test_draft_ready_is_not_collecting(self) -> None:
        run = SimpleNamespace(status="needs_user", current_step=AUTO_DRAFT_READY)
        assert is_auto_collecting_context(run) is False

    def test_none_returns_false(self) -> None:
        assert is_auto_collecting_context(None) is False


class TestBuildAutoStageButtons:
    def test_collecting_context_buttons_are_non_executing(self) -> None:
        buttons = build_auto_stage_buttons(42, AUTO_COLLECTING_CONTEXT)
        labels = _labels(buttons)
        actions = _actions(buttons)

        assert "生成最终方案" in labels
        assert "查看当前草稿" in labels
        assert "取消" in labels
        # Must NOT contain execution buttons
        assert "交给 Claude 执行" not in labels
        assert "Codex 验收" not in labels
        # Actions must be proper
        assert AUTO_FINAL_PLAN in actions
        assert AUTO_SHOW_DRAFT in actions
        assert AUTO_CANCEL in actions

    def test_draft_ready_buttons_expose_claude_gate_only_with_visible_plan(self) -> None:
        buttons = build_auto_stage_buttons(
            42,
            AUTO_DRAFT_READY,
            last_codex_analysis="最终方案：\n1. 修改代码。\n2. 跑验收。",
        )
        labels = _labels(buttons)
        actions = _actions(buttons)

        assert "交给 Claude 执行" in labels
        assert "继续补充" in labels
        assert "重写方案" in labels
        assert "查看当前草稿" in labels
        assert "Codex 接管修" in labels
        assert "结束任务" in labels
        assert AUTO_SEND_TO_CLAUDE in actions
        assert AUTO_CONTINUE_CONTEXT in actions
        assert AUTO_REWRITE_PLAN in actions
        assert AUTO_SHOW_DRAFT in actions
        assert AUTO_CODEX_TAKEOVER in actions
        assert AUTO_CLOSE in actions

    def test_draft_ready_without_visible_plan_suppresses_claude_gate(self) -> None:
        buttons = build_auto_stage_buttons(42, AUTO_DRAFT_READY)
        labels = _labels(buttons)
        actions = _actions(buttons)

        assert "交给 Claude 执行" not in labels
        assert AUTO_SEND_TO_CLAUDE not in actions
        assert "重写方案" in labels
        assert "继续补充" in labels
        assert "结束任务" in labels

    def test_draft_ready_no_implementation_suppresses_claude_gate(self) -> None:
        """When Codex says needs_implementation: false, draft_ready must not
        offer Claude execution; must offer 结束任务 and 继续问 Codex."""
        buttons = build_auto_stage_buttons(
            42, AUTO_DRAFT_READY,
            last_codex_analysis="needs_implementation: false\n无需修改代码。",
        )
        labels = _labels(buttons)
        actions = _actions(buttons)

        assert "交给 Claude 执行" not in labels
        assert AUTO_SEND_TO_CLAUDE not in actions
        assert "继续问 Codex" in labels
        assert "结束任务" in labels
        assert AUTO_CONTINUE_CONTEXT in actions
        assert AUTO_CLOSE in actions

    def test_claude_running_buttons(self) -> None:
        buttons = build_auto_stage_buttons(42, AUTO_CLAUDE_RUNNING)
        labels = _labels(buttons)
        assert "查看状态" in labels
        assert "打断 Claude" in labels

    def test_claude_done_buttons(self) -> None:
        buttons = build_auto_stage_buttons(42, AUTO_CLAUDE_DONE)
        labels = _labels(buttons)
        assert "Codex 验收" in labels
        assert "查看 diff" in labels
        assert "发给 Claude 返工" in labels
        assert "Codex 接管修" in labels
        assert "结束任务" in labels

    def test_verifying_buttons(self) -> None:
        buttons = build_auto_stage_buttons(42, AUTO_VERIFYING)
        labels = _labels(buttons)
        assert "查看状态" in labels

    def test_retry_ready_buttons_expose_repair_gate(self) -> None:
        buttons = build_auto_stage_buttons(42, AUTO_RETRY_READY)
        labels = _labels(buttons)
        actions = _actions(buttons)

        assert "发给 Claude 返工" in labels
        assert "继续补充" in labels
        assert "重写返工提示词" in labels
        assert "Codex 接管修" in labels
        assert "结束任务" in labels
        assert AUTO_SEND_REPAIR_TO_CLAUDE in actions
        assert AUTO_CONTINUE_CONTEXT in actions
        assert AUTO_REWRITE_REPAIR in actions
        assert AUTO_CODEX_TAKEOVER in actions
        assert AUTO_CLOSE in actions

    def test_unknown_stage_defaults_to_status(self) -> None:
        buttons = build_auto_stage_buttons(42, "unknown_stage")
        labels = _labels(buttons)
        assert "查看状态" in labels

    def test_button_callback_data_format(self) -> None:
        buttons = build_auto_stage_buttons(
            99,
            AUTO_DRAFT_READY,
            last_codex_analysis="最终方案：执行修复。",
        )
        for row in buttons:
            for btn in row:
                assert btn["callback_data"].startswith("conv:99:")

    def test_no_vague_continue_label(self) -> None:
        """No button label should be just '继续'."""
        for stage in AUTO_STAGE_STEPS:
            buttons = build_auto_stage_buttons(1, stage)
            for row in buttons:
                for btn in row:
                    assert btn["text"] != "继续", (
                        f"Stage {stage} has vague '继续' button"
                    )

    def test_collecting_context_no_claude_button(self) -> None:
        """Collecting context must not offer any Claude execution button."""
        buttons = build_auto_stage_buttons(42, AUTO_COLLECTING_CONTEXT)
        actions = _actions(buttons)
        assert AUTO_SEND_TO_CLAUDE not in actions
        assert AUTO_SEND_REPAIR_TO_CLAUDE not in actions


class TestAutoStageLabel:
    def test_known_stages(self) -> None:
        assert "Codex" in auto_stage_label(AUTO_COLLECTING_CONTEXT)
        assert "收集" in auto_stage_label(AUTO_COLLECTING_CONTEXT)
        assert "方案" in auto_stage_label(AUTO_DRAFT_READY)
        assert "执行" in auto_stage_label(AUTO_CLAUDE_RUNNING)
        assert "验收" in auto_stage_label(AUTO_CLAUDE_DONE)

    def test_unknown_stage_returns_raw(self) -> None:
        assert auto_stage_label("unknown") == "unknown"
