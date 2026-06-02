from __future__ import annotations

import pytest

from wlcodex.council import (
    CouncilConfig,
    CouncilReviewPacket,
    CouncilReviewRequest,
    CouncilReviewResult,
    CouncilReviewService,
    CouncilSeat,
    CouncilSeatAssignment,
    build_council_seats,
    default_council_config,
    default_council_seat_definitions,
)


class RecordingReviewer:
    def __init__(self) -> None:
        self.requests: list[CouncilReviewRequest] = []

    async def review(self, request: CouncilReviewRequest) -> CouncilReviewResult:
        self.requests.append(request)
        return CouncilReviewResult(
            seat_id=request.seat.seat_id,
            provider=request.seat.provider,
            model=request.seat.model,
            verdict="approve_with_changes",
            confidence=0.8,
            summary=f"{request.seat.role} reviewed {request.packet.title}",
        )


def _packet() -> CouncilReviewPacket:
    return CouncilReviewPacket(
        title="Callable Council",
        proposal="Expose the council as a small service that callers can reuse.",
        constraints=("Keep native provider details outside the service.",),
    )


@pytest.mark.asyncio
async def test_service_runs_packet_through_configured_seats() -> None:
    reviewer = RecordingReviewer()
    config = CouncilConfig(
        seat_definitions=default_council_seat_definitions(),
        assignments=(
            CouncilSeatAssignment(
                seat_id="contrarian",
                provider="codex",
                model="gpt-5.5",
            ),
            CouncilSeatAssignment(
                seat_id="executor",
                provider="opencode",
                model="glm",
            ),
        ),
        required_seat_ids=("contrarian", "executor"),
    )

    board = await CouncilReviewService(reviewer=reviewer).review_packet(
        packet=_packet(),
        config=config,
    )

    assert board.status == "completed"
    assert [request.seat.seat_id for request in reviewer.requests] == [
        "contrarian",
        "executor",
    ]
    assert [request.seat.provider for request in reviewer.requests] == [
        "codex",
        "opencode",
    ]
    assert {request.packet.fingerprint for request in reviewer.requests} == {
        _packet().fingerprint
    }


@pytest.mark.asyncio
async def test_service_builds_packet_from_plain_fields_for_callers() -> None:
    reviewer = RecordingReviewer()
    config = default_council_config(provider="codex", model="gpt-5.5")

    board = await CouncilReviewService(reviewer=reviewer).review(
        title="Plain Inputs",
        proposal="Let API and CLI callers avoid constructing dataclasses by hand.",
        config=config,
        context="Called from an HTTP handler.",
        success_criteria=("All five seats receive the same packet.",),
        constraints=("The service must not know provider internals.",),
        metadata={"source": "api"},
    )

    assert board.status == "completed"
    assert len(board.results) == 5
    first_packet = reviewer.requests[0].packet
    assert first_packet.title == "Plain Inputs"
    assert first_packet.context == "Called from an HTTP handler."
    assert first_packet.success_criteria == (
        "All five seats receive the same packet.",
    )
    assert first_packet.metadata["source"] == "api"


@pytest.mark.asyncio
async def test_service_allows_custom_seat_builder_for_non_config_callers() -> None:
    reviewer = RecordingReviewer()
    custom_seat = CouncilSeat(
        seat_id="one-off-reviewer",
        provider="codex",
        model="gpt-5.5",
        role="ad hoc reviewer",
    )

    def custom_builder(config: CouncilConfig) -> tuple[CouncilSeat, ...]:
        assert config.mode == "quick"
        return (custom_seat,)

    board = await CouncilReviewService(
        reviewer=reviewer,
        seat_builder=custom_builder,
    ).review_packet(
        packet=_packet(),
        config=CouncilConfig(mode="quick"),
    )

    assert board.status == "completed"
    assert [request.seat.seat_id for request in reviewer.requests] == [
        "one-off-reviewer",
    ]


@pytest.mark.asyncio
async def test_service_rejects_configs_with_no_enabled_seats() -> None:
    config = CouncilConfig(
        seat_definitions=default_council_seat_definitions(),
        assignments=(),
    )

    with pytest.raises(ValueError, match="council review requires at least one enabled seat"):
        await CouncilReviewService(reviewer=RecordingReviewer()).review_packet(
            packet=_packet(),
            config=config,
        )


def test_default_builder_is_the_public_config_builder() -> None:
    assert CouncilReviewService.default_seat_builder is build_council_seats
