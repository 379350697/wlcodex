from __future__ import annotations

import asyncio
from typing import Protocol

from wlcodex.council.models import (
    CouncilReviewBoard,
    CouncilReviewPacket,
    CouncilReviewRequest,
    CouncilReviewResult,
    CouncilSeat,
    CouncilSynthesis,
)


class CouncilReviewer(Protocol):
    async def review(self, request: CouncilReviewRequest) -> CouncilReviewResult: ...


class CouncilOrchestrator:
    def __init__(self, reviewer: CouncilReviewer) -> None:
        self._reviewer = reviewer

    async def run_blind_review(
        self,
        *,
        packet: CouncilReviewPacket,
        seats: tuple[CouncilSeat, ...],
    ) -> CouncilReviewBoard:
        ordered_seats = tuple(seats)
        _ensure_unique_seats(ordered_seats)
        tasks = [
            self._review_seat(
                CouncilReviewRequest(
                    packet=packet,
                    seat=seat,
                    round_index=1,
                    peer_reviews=(),
                )
            )
            for seat in ordered_seats
        ]
        results = tuple(await asyncio.gather(*tasks))
        synthesis = CouncilSynthesis.from_results(results)
        return CouncilReviewBoard(
            packet_fingerprint=packet.fingerprint,
            seats=ordered_seats,
            results=results,
            synthesis=synthesis,
            status=_board_status(results),
            round_index=1,
        )

    async def _review_seat(
        self,
        request: CouncilReviewRequest,
    ) -> CouncilReviewResult:
        try:
            return await self._reviewer.review(request)
        except Exception as exc:
            return CouncilReviewResult.failed(request.seat, str(exc))


def _ensure_unique_seats(seats: tuple[CouncilSeat, ...]) -> None:
    seen: set[str] = set()
    for seat in seats:
        if seat.seat_id in seen:
            raise ValueError(f"duplicate council seat: {seat.seat_id}")
        seen.add(seat.seat_id)


def _board_status(results: tuple[CouncilReviewResult, ...]) -> str:
    completed = sum(1 for result in results if result.status == "completed")
    failed = sum(1 for result in results if result.status == "failed")
    if completed == len(results):
        return "completed"
    if failed == len(results):
        return "failed"
    return "partial"
