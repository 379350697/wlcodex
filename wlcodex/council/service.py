from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from wlcodex.council.config import CouncilConfig, build_council_seats
from wlcodex.council.models import (
    CouncilReviewBoard,
    CouncilReviewPacket,
    CouncilSeat,
)
from wlcodex.council.orchestrator import CouncilOrchestrator, CouncilReviewer


class CouncilSeatBuilder(Protocol):
    def __call__(self, config: CouncilConfig) -> tuple[CouncilSeat, ...]: ...


class CouncilReviewService:
    default_seat_builder: CouncilSeatBuilder = staticmethod(build_council_seats)

    def __init__(
        self,
        *,
        reviewer: CouncilReviewer,
        seat_builder: CouncilSeatBuilder | None = None,
    ) -> None:
        self._orchestrator = CouncilOrchestrator(reviewer=reviewer)
        self._seat_builder = seat_builder or self.default_seat_builder

    async def review_packet(
        self,
        *,
        packet: CouncilReviewPacket,
        config: CouncilConfig,
    ) -> CouncilReviewBoard:
        seats = self._seat_builder(config)
        if not seats:
            raise ValueError("council review requires at least one enabled seat")
        return await self._orchestrator.run_blind_review(
            packet=packet,
            seats=seats,
        )

    async def review(
        self,
        *,
        title: str,
        proposal: str,
        config: CouncilConfig,
        context: str = "",
        success_criteria: tuple[str, ...] = (),
        constraints: tuple[str, ...] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> CouncilReviewBoard:
        packet = CouncilReviewPacket(
            title=title,
            proposal=proposal,
            context=context,
            success_criteria=success_criteria,
            constraints=constraints,
            metadata=metadata or {},
        )
        return await self.review_packet(packet=packet, config=config)
