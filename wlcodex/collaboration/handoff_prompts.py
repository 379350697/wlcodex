from __future__ import annotations

from collections.abc import Iterable

from wlcodex.collaboration.models import (
    HandoffArtifact,
    HandoffIntent,
    HandoffPreviewInput,
    HandoffPromptPreview,
    IntentDetectionResult,
)


BUG_MARKERS = (
    "bug",
    "error",
    "failure",
    "failed",
    "regression",
    "unexpected",
    "traceback",
    "stack trace",
    "notimplementederror",
    "报错",
    "失败",
    "问题",
    "不一致",
)

FEATURE_MARKERS = (
    "add",
    "build",
    "implement",
    "change",
    "新增",
    "加一个",
    "实现",
    "改成",
)

MAX_SUMMARY_CHARS = 1200
_MAX_RENDERED_CONTEXT_CHARS = 760


def detect_handoff_intent(
    *,
    recent_user_text: str,
    session_summary: str,
    artifacts: list[HandoffArtifact],
) -> IntentDetectionResult:
    kinds = {artifact.kind.lower() for artifact in artifacts}
    paths = " ".join(artifact.path.lower() for artifact in artifacts)
    newest_text = recent_user_text.lower()
    if any(marker in newest_text for marker in BUG_MARKERS):
        return IntentDetectionResult(
            HandoffIntent.FIX_BUG,
            "high",
            "Bug or error language was detected in the newest user request.",
        )
    if {"spec", "plan"}.issubset(kinds) or (
        "docs/superpowers/specs/" in paths
        and "docs/superpowers/plans/" in paths
    ):
        return IntentDetectionResult(
            HandoffIntent.EXECUTE_PLAN,
            "high",
            "Spec and plan artifacts were detected.",
        )
    text = f"{recent_user_text}\n{session_summary}".lower()
    if any(marker in text for marker in BUG_MARKERS):
        return IntentDetectionResult(
            HandoffIntent.FIX_BUG,
            "high",
            "Bug or error language was detected.",
        )
    if any(marker in text for marker in FEATURE_MARKERS):
        return IntentDetectionResult(
            HandoffIntent.IMPLEMENT_FEATURE,
            "medium",
            "Feature implementation language was detected.",
        )
    return IntentDetectionResult(
        HandoffIntent.CONTINUE_WORK,
        "low",
        "No specific plan, bug, or feature signal was detected.",
    )


def build_handoff_preview(request: HandoffPreviewInput) -> HandoffPromptPreview:
    detection = detect_handoff_intent(
        recent_user_text=request.recent_user_text,
        session_summary=request.session_summary,
        artifacts=request.artifacts,
    )
    intent = _requested_intent_or_detected(request.requested_intent, detection.intent)
    prompt = _render_prompt(request, intent)
    return HandoffPromptPreview(
        intent=intent,
        target_provider=request.target_provider,
        prompt=prompt,
        artifacts=list(request.artifacts),
        warnings=_warnings(request, detection),
        reason=(
            f"User requested intent: {intent.value}."
            if intent != detection.intent
            else detection.reason
        ),
    )


def _requested_intent_or_detected(
    requested: HandoffIntent,
    detected: HandoffIntent,
) -> HandoffIntent:
    if requested == HandoffIntent.AUTO:
        return detected
    return requested


def _render_prompt(request: HandoffPreviewInput, intent: HandoffIntent) -> str:
    sections = [
        _metadata_section(request),
        _newest_request_section(request.recent_user_text),
    ]
    summary = _context_section(request.session_summary)
    if summary:
        sections.append(summary)
    artifacts = _artifact_section(request.artifacts)
    if artifacts:
        sections.append(artifacts)
    if request.user_note.strip():
        sections.append(f"User note:\n{_compact_text(request.user_note)}")
    sections.append(_intent_template(intent, request))
    sections.append(
        "Final response: provide a concise summary with changed files, "
        "verification result, commands run, and remaining risks."
    )
    return "\n\n".join(section for section in sections if section.strip())


def _metadata_section(request: HandoffPreviewInput) -> str:
    return "\n".join(
        [
            f"Workspace: {request.cwd}",
            f"Source provider: {request.source_provider}",
            f"Source thread: {request.source_thread_id}",
            f"Target provider: {request.target_provider}",
        ]
    )


def _newest_request_section(text: str) -> str:
    content = _compact_text(text) if text.strip() else "(none provided)"
    return f"Newest user request:\n{content}"


def _context_section(text: str) -> str:
    if not text.strip():
        return ""
    return f"Compact session summary:\n{_compact_text(text)}"


def _artifact_section(artifacts: Iterable[HandoffArtifact]) -> str:
    lines = []
    for artifact in artifacts:
        title = f" ({artifact.title})" if artifact.title else ""
        lines.append(f"- {artifact.kind}{title}: {artifact.path}")
    if not lines:
        return ""
    return "Artifacts to read:\n" + "\n".join(lines)


def _intent_template(intent: HandoffIntent, request: HandoffPreviewInput) -> str:
    if intent == HandoffIntent.EXECUTE_PLAN:
        return _execute_plan_template(request.artifacts)
    if intent == HandoffIntent.FIX_BUG:
        return _fix_bug_template()
    if intent == HandoffIntent.IMPLEMENT_FEATURE:
        return _implement_feature_template()
    if intent == HandoffIntent.CUSTOM:
        return _custom_template()
    return _continue_work_template()


def _execute_plan_template(artifacts: list[HandoffArtifact]) -> str:
    spec_paths = _artifact_paths(artifacts, "spec")
    plan_paths = _artifact_paths(artifacts, "plan")
    specific_paths = []
    if spec_paths:
        specific_paths.append("Spec paths: " + ", ".join(spec_paths))
    if plan_paths:
        specific_paths.append("Plan paths: " + ", ".join(plan_paths))
    path_text = "\n".join(specific_paths)
    return "\n".join(
        [
            "Task: execute the plan.",
            "Read the exact spec and plan paths before editing.",
            path_text,
            "Do not rewrite the plan unless the user explicitly asks for a new plan.",
            "Preserve unrelated changes in the workspace.",
            "Run focused tests or another concrete verification command.",
        ]
    ).strip()


def _fix_bug_template() -> str:
    return "\n".join(
        [
            "Task: fix the bug described by the newest user request.",
            "Reproduce or inspect evidence first before changing code.",
            "Prefer a failing test before the fix when practical.",
            "Identify and report the root cause.",
            "Preserve unrelated changes in the workspace.",
            "Report files changed and verification results.",
        ]
    )


def _implement_feature_template() -> str:
    return "\n".join(
        [
            "Task: implement the requested feature.",
            "Inspect existing code patterns first.",
            "Keep scope narrow and avoid unrelated refactors.",
            "Add focused tests where risk justifies them.",
            "Report files changed and verification results.",
        ]
    )


def _continue_work_template() -> str:
    return "\n".join(
        [
            "Task: continue the current work from the newest user request.",
            "Use known artifacts as references, not as transcript replay.",
            "Avoid replaying old assistant output as fresh instructions.",
            "Preserve unrelated changes and keep the next step focused.",
            "Report files changed and verification results.",
        ]
    )


def _custom_template() -> str:
    return "\n".join(
        [
            "Task: follow the user's edited custom handoff prompt.",
            "Use the workspace, source provider, source thread, and target provider",
            "metadata above for traceability.",
            "Do not override the user's edited task with generated assumptions.",
            "Report files changed and verification results.",
        ]
    )


def _artifact_paths(artifacts: Iterable[HandoffArtifact], kind: str) -> list[str]:
    return [
        artifact.path
        for artifact in artifacts
        if artifact.kind.lower() == kind or f"/{kind}s/" in artifact.path.lower()
    ]


def _compact_text(text: str) -> str:
    compact = text.strip()
    limit = min(MAX_SUMMARY_CHARS, _MAX_RENDERED_CONTEXT_CHARS)
    if len(compact) <= limit:
        return compact
    return "[truncated]\n" + compact[-limit:].lstrip()


def _warnings(
    request: HandoffPreviewInput,
    detection: IntentDetectionResult,
) -> list[str]:
    warnings = []
    if detection.confidence == "low":
        warnings.append(detection.reason)
    if _has_spec_and_plan(request.artifacts) and detection.intent == HandoffIntent.FIX_BUG:
        warnings.append(
            "Spec and plan artifacts were detected, but the newest user request "
            "looks like a bug or error."
        )
    if request.requested_intent == HandoffIntent.EXECUTE_PLAN and not _has_spec_and_plan(
        request.artifacts
    ):
        warnings.append("execute_plan requested without spec and plan artifacts.")
    if request.requested_intent == HandoffIntent.CUSTOM and not request.user_note.strip():
        warnings.append("custom handoff requested without a user note.")
    if not request.cwd.strip():
        warnings.append("Missing workspace path.")
    if not request.source_provider.strip():
        warnings.append("Missing source provider.")
    if not request.target_provider.strip():
        warnings.append("Missing target provider.")
    return warnings


def _has_spec_and_plan(artifacts: list[HandoffArtifact]) -> bool:
    kinds = {artifact.kind.lower() for artifact in artifacts}
    paths = " ".join(artifact.path.lower() for artifact in artifacts)
    return {"spec", "plan"}.issubset(kinds) or (
        "docs/superpowers/specs/" in paths
        and "docs/superpowers/plans/" in paths
    )
