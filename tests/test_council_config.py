from __future__ import annotations

import pytest

from wlcodex.council import (
    CouncilConfig,
    CouncilSeatAssignment,
    build_council_seats,
    council_assignment_diversity,
    default_council_config,
    default_council_seat_definitions,
)


def test_default_council_config_builds_five_logical_seats() -> None:
    config = default_council_config(
        provider="codex",
        model="gpt-5.5",
    )

    seats = build_council_seats(config)

    assert [seat.seat_id for seat in seats] == [
        "contrarian",
        "first_principles",
        "expander",
        "outsider",
        "executor",
    ]
    assert [seat.role for seat in seats] == [
        "唱反调",
        "第一性原理",
        "扩展思路",
        "局外人",
        "执行者",
    ]
    assert {seat.provider for seat in seats} == {"codex"}
    assert {seat.model for seat in seats} == {"gpt-5.5"}
    assert seats[0].metadata["mission"]
    assert seats[0].metadata["required_outputs"] == (
        "hard_blockers",
        "counterexamples",
        "minimum_required_fix",
    )


def test_assignments_allow_one_model_to_fill_multiple_seats() -> None:
    definitions = default_council_seat_definitions()
    config = CouncilConfig(
        seat_definitions=definitions,
        assignments=(
            CouncilSeatAssignment(
                seat_id="contrarian",
                provider="claude",
                model="deepseek-v4",
            ),
            CouncilSeatAssignment(
                seat_id="first_principles",
                provider="claude",
                model="deepseek-v4",
            ),
            CouncilSeatAssignment(
                seat_id="expander",
                provider="antigravity",
                model="opus-4.6",
            ),
        ),
        required_seat_ids=("contrarian", "first_principles"),
    )

    seats = build_council_seats(config)

    assert [seat.seat_id for seat in seats] == [
        "contrarian",
        "first_principles",
        "expander",
    ]
    assert seats[0].provider == "claude"
    assert seats[1].provider == "claude"
    assert seats[0].model == "deepseek-v4"
    assert seats[1].model == "deepseek-v4"
    assert seats[2].provider == "antigravity"
    assert seats[2].metadata["assignment_provider_model"] == "antigravity:opus-4.6"


def test_disabled_assignments_are_ignored_but_required_seats_must_be_enabled() -> None:
    config = CouncilConfig(
        seat_definitions=default_council_seat_definitions(),
        assignments=(
            CouncilSeatAssignment(
                seat_id="contrarian",
                provider="codex",
                model="gpt-5.5",
                enabled=False,
            ),
        ),
        required_seat_ids=("contrarian",),
    )

    with pytest.raises(ValueError, match="required council seat is not assigned: contrarian"):
        build_council_seats(config)


def test_council_config_rejects_unknown_seat_assignment() -> None:
    config = CouncilConfig(
        seat_definitions=default_council_seat_definitions(),
        assignments=(
            CouncilSeatAssignment(
                seat_id="phantom",
                provider="codex",
                model="gpt-5.5",
            ),
        ),
    )

    with pytest.raises(ValueError, match="unknown council seat assignment: phantom"):
        build_council_seats(config)


def test_council_assignment_diversity_reports_reuse_without_blocking_it() -> None:
    config = default_council_config(provider="codex", model="gpt-5.5")

    diversity = council_assignment_diversity(config.assignments)

    assert diversity.enabled_seats == 5
    assert diversity.unique_models == 1
    assert diversity.score == 0.2
    assert diversity.is_single_model is True
