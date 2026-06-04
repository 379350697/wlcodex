from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class HandoffIntent(StrEnum):
    AUTO = "auto"
    EXECUTE_PLAN = "execute_plan"
    FIX_BUG = "fix_bug"
    IMPLEMENT_FEATURE = "implement_feature"
    CONTINUE_WORK = "continue_work"
    CUSTOM = "custom"


@dataclass(frozen=True)
class HandoffArtifact:
    kind: str
    path: str
    title: str = ""
    source: str = ""
    confidence: str = "medium"

    def to_json_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "path": self.path,
            "title": self.title,
            "source": self.source,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class IntentDetectionResult:
    intent: HandoffIntent
    confidence: str
    reason: str


@dataclass(frozen=True)
class HandoffPreviewInput:
    source_provider: str
    source_thread_id: str
    target_provider: str
    cwd: str
    recent_user_text: str = ""
    session_summary: str = ""
    artifacts: list[HandoffArtifact] = field(default_factory=list)
    user_note: str = ""
    requested_intent: HandoffIntent = HandoffIntent.AUTO


@dataclass(frozen=True)
class HandoffPromptPreview:
    intent: HandoffIntent
    target_provider: str
    prompt: str
    artifacts: list[HandoffArtifact] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reason: str = ""

    def to_json_dict(self) -> dict[str, object]:
        return {
            "intent": self.intent.value,
            "target_provider": self.target_provider,
            "prompt": self.prompt,
            "artifacts": [artifact.to_json_dict() for artifact in self.artifacts],
            "warnings": list(self.warnings),
            "reason": self.reason,
        }
