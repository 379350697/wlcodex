from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Observation:
    team_run_id: int
    domain: str
    summary: str
    evidence_refs: tuple[str, ...]
    confidence: float


@dataclass(frozen=True)
class InstinctMemory:
    instinct_id: str
    scope: str
    workspace_alias: str | None
    role: str
    domain: str
    trigger: str
    action: str
    confidence: float
    evidence_refs: tuple[str, ...]
    status: str
    created_at: datetime
    last_validated_at: datetime

    def as_packet_item(self) -> dict[str, object]:
        return {
            "id": self.instinct_id,
            "scope": self.scope,
            "role": self.role,
            "trigger": self.trigger,
            "action": self.action,
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
            "precedence": "historical_advice_current_evidence_wins",
        }


def _text_score(needle: str, haystack: str) -> int:
    words = [
        word for word in needle.lower().replace("-", " ").split() if len(word) >= 4
    ]
    lowered = haystack.lower()
    return sum(1 for word in words if word in lowered)


def _scope_score(instinct: InstinctMemory, workspace_alias: str) -> int:
    if (
        instinct.scope in {"project", "workspace"}
        and instinct.workspace_alias == workspace_alias
    ):
        return 3
    if instinct.scope == "global":
        return 1
    return 0


def select_relevant_instincts(
    instincts: tuple[InstinctMemory, ...],
    *,
    workspace_alias: str,
    role: str,
    task_text: str,
    limit: int = 3,
    min_confidence: float = 0.6,
) -> tuple[InstinctMemory, ...]:
    scored: list[tuple[int, float, str, InstinctMemory]] = []
    for instinct in instincts:
        if instinct.status != "active":
            continue
        if instinct.confidence < min_confidence:
            continue
        if instinct.role not in {role, "*"}:
            continue
        scope_score = _scope_score(instinct, workspace_alias)
        if scope_score == 0:
            continue
        relevance = _text_score(instinct.trigger, task_text) + _text_score(
            instinct.domain, task_text
        )
        if relevance == 0:
            continue
        scored.append(
            (scope_score + relevance, instinct.confidence, instinct.instinct_id, instinct)
        )

    ordered = sorted(scored, key=lambda item: (-item[0], -item[1], item[2]))
    return tuple(item[3] for item in ordered[:limit])
