from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
from types import MappingProxyType
from typing import Any, Literal


CouncilVerdict = Literal[
    "approve",
    "approve_with_changes",
    "reject",
    "need_more_info",
]
CouncilResultStatus = Literal["completed", "started", "failed"]
CouncilBoardStatus = Literal["completed", "partial", "failed"]


def _tuple_of_strings(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(str(value) for value in values if str(value).strip())


@dataclass(frozen=True)
class CouncilReviewPacket:
    title: str
    proposal: str
    context: str = ""
    success_criteria: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", self.title.strip())
        object.__setattr__(self, "proposal", self.proposal.strip())
        object.__setattr__(self, "context", self.context.strip())
        object.__setattr__(
            self,
            "success_criteria",
            _tuple_of_strings(tuple(self.success_criteria)),
        )
        object.__setattr__(
            self,
            "constraints",
            _tuple_of_strings(tuple(self.constraints)),
        )
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        if not self.title:
            raise ValueError("council review packet title cannot be empty")
        if not self.proposal:
            raise ValueError("council review packet proposal cannot be empty")

    @property
    def fingerprint(self) -> str:
        payload = {
            "title": self.title,
            "proposal": self.proposal,
            "context": self.context,
            "success_criteria": list(self.success_criteria),
            "constraints": list(self.constraints),
            "metadata": dict(self.metadata),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()

    def render_for_review(self) -> str:
        sections = [
            "# Council Review Packet",
            f"title: {self.title}",
            "",
            "## Proposal",
            self.proposal,
        ]
        if self.context:
            sections.extend(["", "## Context", self.context])
        if self.success_criteria:
            sections.extend(
                ["", "## Success Criteria"]
                + [f"- {item}" for item in self.success_criteria]
            )
        if self.constraints:
            sections.extend(
                ["", "## Constraints"] + [f"- {item}" for item in self.constraints]
            )
        sections.extend(
            [
                "",
                "## Required Review Shape",
                "Return verdict, confidence, top risks, required changes, and open questions.",
            ]
        )
        return "\n".join(sections)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "proposal": self.proposal,
            "context": self.context,
            "success_criteria": list(self.success_criteria),
            "constraints": list(self.constraints),
            "metadata": dict(self.metadata),
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class CouncilSeat:
    seat_id: str
    provider: str
    model: str
    role: str
    profile: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "seat_id", self.seat_id.strip())
        object.__setattr__(self, "provider", self.provider.strip())
        object.__setattr__(self, "model", self.model.strip())
        object.__setattr__(self, "role", self.role.strip())
        object.__setattr__(self, "profile", self.profile.strip())
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        if not self.seat_id:
            raise ValueError("council seat id cannot be empty")
        if not self.provider:
            raise ValueError("council seat provider cannot be empty")
        if not self.model:
            raise ValueError("council seat model cannot be empty")
        if not self.role:
            raise ValueError("council seat role cannot be empty")

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "seat_id": self.seat_id,
            "provider": self.provider,
            "model": self.model,
            "role": self.role,
            "profile": self.profile,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class CouncilReviewResult:
    seat_id: str
    provider: str
    model: str
    verdict: CouncilVerdict | str
    confidence: float
    summary: str
    risks: tuple[str, ...] = ()
    required_changes: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    raw_output: str = ""
    status: CouncilResultStatus = "completed"
    error: str = ""
    native_session_id: str = ""
    provider_engine: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "seat_id", self.seat_id.strip())
        object.__setattr__(self, "provider", self.provider.strip())
        object.__setattr__(self, "model", self.model.strip())
        object.__setattr__(self, "verdict", str(self.verdict).strip())
        object.__setattr__(self, "summary", self.summary.strip())
        object.__setattr__(self, "risks", _tuple_of_strings(tuple(self.risks)))
        object.__setattr__(
            self,
            "required_changes",
            _tuple_of_strings(tuple(self.required_changes)),
        )
        object.__setattr__(
            self,
            "open_questions",
            _tuple_of_strings(tuple(self.open_questions)),
        )
        object.__setattr__(self, "raw_output", self.raw_output.strip())
        object.__setattr__(self, "error", self.error.strip())
        object.__setattr__(self, "native_session_id", self.native_session_id.strip())
        object.__setattr__(self, "provider_engine", self.provider_engine.strip())
        if self.status not in {"completed", "started", "failed"}:
            raise ValueError(
                "council review result status must be completed, started, or failed"
            )
        if not self.seat_id:
            raise ValueError("council review result seat id cannot be empty")
        if not self.provider:
            raise ValueError("council review result provider cannot be empty")
        if not self.model:
            raise ValueError("council review result model cannot be empty")

    @classmethod
    def failed(cls, seat: CouncilSeat, error: str) -> CouncilReviewResult:
        return cls(
            seat_id=seat.seat_id,
            provider=seat.provider,
            model=seat.model,
            verdict="need_more_info",
            confidence=0.0,
            summary="",
            status="failed",
            error=error,
        )

    @classmethod
    def started(
        cls,
        seat: CouncilSeat,
        *,
        native_session_id: str,
        provider_engine: str,
    ) -> CouncilReviewResult:
        return cls(
            seat_id=seat.seat_id,
            provider=seat.provider,
            model=seat.model,
            verdict="need_more_info",
            confidence=0.0,
            summary=f"Native review session started: {native_session_id}",
            status="started",
            raw_output=native_session_id,
            native_session_id=native_session_id,
            provider_engine=provider_engine,
            open_questions=(f"Sync {seat.provider}/{provider_engine} review output.",),
        )

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "seat_id": self.seat_id,
            "provider": self.provider,
            "model": self.model,
            "verdict": self.verdict,
            "confidence": self.confidence,
            "summary": self.summary,
            "risks": list(self.risks),
            "required_changes": list(self.required_changes),
            "open_questions": list(self.open_questions),
            "raw_output": self.raw_output,
            "status": self.status,
            "error": self.error,
            "native_session_id": self.native_session_id,
            "provider_engine": self.provider_engine,
        }


@dataclass(frozen=True)
class CouncilReviewRequest:
    packet: CouncilReviewPacket
    seat: CouncilSeat
    round_index: int = 1
    peer_reviews: tuple[CouncilReviewResult, ...] = ()

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "packet": self.packet.to_json_dict(),
            "seat": self.seat.to_json_dict(),
            "round_index": self.round_index,
            "peer_reviews": [review.to_json_dict() for review in self.peer_reviews],
        }


@dataclass(frozen=True)
class CouncilSynthesis:
    verdict_counts: dict[str, int]
    consensus: str
    required_changes: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    failed_seats: tuple[str, ...] = ()

    @classmethod
    def from_results(cls, results: tuple[CouncilReviewResult, ...]) -> CouncilSynthesis:
        verdict_counts: dict[str, int] = {}
        required_changes: list[str] = []
        risks: list[str] = []
        failed_seats: list[str] = []
        for result in results:
            if result.status == "failed":
                failed_seats.append(result.seat_id)
                continue
            if result.status != "completed":
                continue
            verdict_counts[result.verdict] = verdict_counts.get(result.verdict, 0) + 1
            required_changes.extend(result.required_changes)
            risks.extend(result.risks)
        if not verdict_counts:
            consensus = "no_completed_reviews"
        elif len(verdict_counts) == 1:
            consensus = next(iter(verdict_counts))
        else:
            consensus = "mixed"
        return cls(
            verdict_counts=verdict_counts,
            consensus=consensus,
            required_changes=tuple(required_changes),
            risks=tuple(risks),
            failed_seats=tuple(failed_seats),
        )

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "verdict_counts": self.verdict_counts,
            "consensus": self.consensus,
            "required_changes": list(self.required_changes),
            "risks": list(self.risks),
            "failed_seats": list(self.failed_seats),
        }


@dataclass(frozen=True)
class CouncilReviewBoard:
    packet_fingerprint: str
    seats: tuple[CouncilSeat, ...]
    results: tuple[CouncilReviewResult, ...]
    synthesis: CouncilSynthesis
    status: CouncilBoardStatus
    round_index: int = 1

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "packet_fingerprint": self.packet_fingerprint,
            "round_index": self.round_index,
            "status": self.status,
            "seats": [seat.to_json_dict() for seat in self.seats],
            "results": [result.to_json_dict() for result in self.results],
            "synthesis": self.synthesis.to_json_dict(),
        }
