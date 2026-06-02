from __future__ import annotations

import pytest

from wlcodex.council import (
    CouncilOrchestrator,
    CouncilReviewPacket,
    CouncilReviewRequest,
    CouncilReviewResult,
    CouncilSeat,
)


class RecordingReviewer:
    def __init__(self, *, fail_seat: str = "", started: bool = False) -> None:
        self.fail_seat = fail_seat
        self.started = started
        self.requests: list[CouncilReviewRequest] = []

    async def review(self, request: CouncilReviewRequest) -> CouncilReviewResult:
        self.requests.append(request)
        if request.seat.seat_id == self.fail_seat:
            raise RuntimeError("review backend unavailable")
        if self.started:
            return CouncilReviewResult.started(
                request.seat,
                native_session_id=f"native-{request.seat.seat_id}",
                provider_engine="app-server",
            )
        return CouncilReviewResult(
            seat_id=request.seat.seat_id,
            provider=request.seat.provider,
            model=request.seat.model,
            verdict="approve_with_changes",
            confidence=0.72,
            summary=f"{request.seat.seat_id} reviewed {request.packet.title}",
            risks=(f"{request.seat.seat_id}: scope drift",),
            required_changes=(f"{request.seat.seat_id}: keep adapters thin",),
        )


def _packet() -> CouncilReviewPacket:
    return CouncilReviewPacket(
        title="LLM Council MVP",
        proposal="Run four reviewer seats against one immutable proposal packet.",
        context="WLCodex already has native provider and adaptive team concepts.",
        success_criteria=(
            "Every reviewer receives the same proposal fingerprint.",
            "One failed reviewer does not cancel the whole council.",
        ),
        constraints=(
            "Review only; do not edit files.",
            "Keep business workflow coupled to Council, adapters replaceable.",
        ),
    )


def _seats() -> tuple[CouncilSeat, ...]:
    return (
        CouncilSeat(
            seat_id="codex-gpt55",
            provider="codex",
            model="gpt-5.5",
            role="chief reviewer",
        ),
        CouncilSeat(
            seat_id="claude-deepseekv4",
            provider="claude",
            model="deepseek-v4",
            role="risk reviewer",
        ),
    )


def test_review_packet_has_stable_fingerprint_and_canonical_prompt() -> None:
    packet = _packet()

    assert packet.fingerprint == _packet().fingerprint
    assert len(packet.fingerprint) == 64
    assert "LLM Council MVP" in packet.render_for_review()
    assert "Review only; do not edit files." in packet.render_for_review()
    assert packet.to_json_dict()["fingerprint"] == packet.fingerprint


def test_review_packet_and_seat_metadata_are_immutable() -> None:
    source_metadata = {"priority": "high"}
    packet = CouncilReviewPacket(
        title="Immutable Packet",
        proposal="Review this exact packet.",
        metadata=source_metadata,
    )
    seat = CouncilSeat(
        seat_id="codex",
        provider="codex",
        model="gpt-5.5",
        role="reviewer",
        metadata=source_metadata,
    )

    source_metadata["priority"] = "low"

    assert packet.metadata["priority"] == "high"
    assert seat.metadata["priority"] == "high"
    with pytest.raises(TypeError):
        packet.metadata["priority"] = "low"
    with pytest.raises(TypeError):
        seat.metadata["priority"] = "low"


@pytest.mark.asyncio
async def test_blind_review_sends_same_packet_to_each_seat_in_order() -> None:
    reviewer = RecordingReviewer()
    board = await CouncilOrchestrator(reviewer=reviewer).run_blind_review(
        packet=_packet(),
        seats=_seats(),
    )

    assert [result.seat_id for result in board.results] == [
        "codex-gpt55",
        "claude-deepseekv4",
    ]
    assert [request.seat.seat_id for request in reviewer.requests] == [
        "codex-gpt55",
        "claude-deepseekv4",
    ]
    assert {request.packet.fingerprint for request in reviewer.requests} == {
        board.packet_fingerprint
    }
    assert all(request.peer_reviews == () for request in reviewer.requests)
    assert board.status == "completed"
    assert board.synthesis.verdict_counts == {"approve_with_changes": 2}
    assert board.synthesis.required_changes == (
        "codex-gpt55: keep adapters thin",
        "claude-deepseekv4: keep adapters thin",
    )


@pytest.mark.asyncio
async def test_blind_review_isolates_one_failed_seat() -> None:
    board = await CouncilOrchestrator(
        reviewer=RecordingReviewer(fail_seat="claude-deepseekv4")
    ).run_blind_review(packet=_packet(), seats=_seats())

    assert board.status == "partial"
    assert [result.status for result in board.results] == ["completed", "failed"]
    assert board.results[1].seat_id == "claude-deepseekv4"
    assert board.results[1].error == "review backend unavailable"
    assert board.synthesis.verdict_counts == {"approve_with_changes": 1}


@pytest.mark.asyncio
async def test_blind_review_marks_all_started_seats_as_partial_not_failed() -> None:
    board = await CouncilOrchestrator(reviewer=RecordingReviewer(started=True)).run_blind_review(
        packet=_packet(),
        seats=_seats(),
    )

    assert board.status == "partial"
    assert [result.status for result in board.results] == ["started", "started"]
    assert board.synthesis.consensus == "no_completed_reviews"


@pytest.mark.asyncio
async def test_blind_review_rejects_duplicate_seat_ids() -> None:
    duplicate = _seats()[0]

    with pytest.raises(ValueError, match="duplicate council seat: codex-gpt55"):
        await CouncilOrchestrator(reviewer=RecordingReviewer()).run_blind_review(
            packet=_packet(),
            seats=(duplicate, duplicate),
        )
