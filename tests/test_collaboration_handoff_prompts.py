from __future__ import annotations

from wlcodex.collaboration.handoff_prompts import (
    build_handoff_preview,
    detect_handoff_intent,
)
from wlcodex.collaboration.models import (
    HandoffArtifact,
    HandoffIntent,
    HandoffPreviewInput,
)


def test_detects_execute_plan_when_spec_and_plan_artifacts_exist() -> None:
    artifacts = [
        HandoffArtifact(kind="spec", path="docs/superpowers/specs/feature.md"),
        HandoffArtifact(kind="plan", path="docs/superpowers/plans/feature.md"),
    ]

    result = detect_handoff_intent(
        recent_user_text="继续",
        session_summary="Antigravity drafted the plan.",
        artifacts=artifacts,
    )

    assert result.intent == HandoffIntent.EXECUTE_PLAN
    assert result.confidence == "high"
    assert "spec" in result.reason.lower()
    assert "plan" in result.reason.lower()


def test_detects_execute_plan_from_spec_and_plan_paths() -> None:
    artifacts = [
        HandoffArtifact(
            kind="unknown",
            path="docs/superpowers/specs/2026-06-04-feature.md",
        ),
        HandoffArtifact(
            kind="unknown",
            path="docs/superpowers/plans/2026-06-04-feature.md",
        ),
    ]

    result = detect_handoff_intent(
        recent_user_text="继续",
        session_summary="Antigravity drafted the plan.",
        artifacts=artifacts,
    )

    assert result.intent == HandoffIntent.EXECUTE_PLAN
    assert result.confidence == "high"


def test_detects_fix_bug_from_error_context() -> None:
    result = detect_handoff_intent(
        recent_user_text="继续的时候出现 NotImplementedError",
        session_summary="The user reports a native provider regression.",
        artifacts=[],
    )

    assert result.intent == HandoffIntent.FIX_BUG
    assert result.confidence == "high"
    assert "bug" in result.reason.lower() or "error" in result.reason.lower()


def test_detects_implement_feature_without_formal_artifacts() -> None:
    result = detect_handoff_intent(
        recent_user_text="新增一个接棒执行按钮",
        session_summary="No spec or plan files were detected.",
        artifacts=[],
    )

    assert result.intent == HandoffIntent.IMPLEMENT_FEATURE
    assert result.confidence in {"medium", "high"}


def test_execute_plan_prompt_references_paths_and_says_execute_not_rewrite() -> None:
    preview = build_handoff_preview(
        HandoffPreviewInput(
            source_provider="antigravity",
            source_thread_id="source-session",
            target_provider="claude",
            cwd="/Users/wl/projects/wlcodex",
            recent_user_text="让 Claude 执行计划",
            session_summary="Antigravity produced a spec and implementation plan.",
            artifacts=[
                HandoffArtifact(
                    kind="spec",
                    path="docs/superpowers/specs/2026-06-04-feature.md",
                ),
                HandoffArtifact(
                    kind="plan",
                    path="docs/superpowers/plans/2026-06-04-feature.md",
                ),
            ],
        )
    )

    assert preview.intent == HandoffIntent.EXECUTE_PLAN
    assert "docs/superpowers/specs/2026-06-04-feature.md" in preview.prompt
    assert "docs/superpowers/plans/2026-06-04-feature.md" in preview.prompt
    assert "execute the plan" in preview.prompt.lower()
    assert "do not rewrite the plan" in preview.prompt.lower()
    assert "/Users/wl/projects/wlcodex" in preview.prompt


def test_bug_prompt_requires_evidence_before_fix() -> None:
    preview = build_handoff_preview(
        HandoffPreviewInput(
            source_provider="codex",
            source_thread_id="bug-source",
            target_provider="antigravity",
            cwd="/repo",
            recent_user_text="按钮状态还是不一致，有 bug",
            session_summary="The source session mentions a UI state mismatch.",
            artifacts=[],
        )
    )

    assert preview.intent == HandoffIntent.FIX_BUG
    assert "reproduce or inspect evidence first" in preview.prompt.lower()
    assert "root cause" in preview.prompt.lower()
    assert "preserve unrelated changes" in preview.prompt.lower()


def test_feature_prompt_keeps_scope_narrow() -> None:
    preview = build_handoff_preview(
        HandoffPreviewInput(
            source_provider="claude",
            source_thread_id="feature-source",
            target_provider="codex",
            cwd="/repo",
            recent_user_text="加一个工作流状态入口",
            session_summary="No formal documents exist.",
            artifacts=[],
        )
    )

    assert preview.intent == HandoffIntent.IMPLEMENT_FEATURE
    assert "inspect existing code patterns first" in preview.prompt.lower()
    assert "keep scope narrow" in preview.prompt.lower()


def test_continue_work_prompt_avoids_replaying_old_assistant_output() -> None:
    preview = build_handoff_preview(
        HandoffPreviewInput(
            source_provider="antigravity",
            source_thread_id="source-session",
            target_provider="claude",
            cwd="/repo",
            recent_user_text="最新请求：只执行新内容",
            session_summary="We have partial work but no formal plan signal.",
            artifacts=[],
        )
    )

    assert preview.intent == HandoffIntent.CONTINUE_WORK
    assert "avoid replaying old assistant output as fresh instructions" in (
        preview.prompt.lower()
    )


def test_prompt_builder_trims_raw_transcript_and_keeps_latest_request() -> None:
    long_transcript = "old assistant output " * 600
    preview = build_handoff_preview(
        HandoffPreviewInput(
            source_provider="antigravity",
            source_thread_id="source-session",
            target_provider="claude",
            cwd="/repo",
            recent_user_text="最新请求：只执行新内容",
            session_summary=long_transcript,
            artifacts=[],
        )
    )

    assert "最新请求：只执行新内容" in preview.prompt
    assert len(preview.prompt) < len(long_transcript)
    assert preview.prompt.count("old assistant output") < 40
