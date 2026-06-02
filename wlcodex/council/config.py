from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from wlcodex.council.models import CouncilSeat


CouncilRunMode = Literal["quick", "council", "debate"]


def _tuple_of_strings(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(str(value).strip() for value in values if str(value).strip())


@dataclass(frozen=True)
class CouncilSeatDefinition:
    seat_id: str
    role: str
    mission: str
    required_outputs: tuple[str, ...]
    veto_rules: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "seat_id", self.seat_id.strip())
        object.__setattr__(self, "role", self.role.strip())
        object.__setattr__(self, "mission", self.mission.strip())
        object.__setattr__(
            self,
            "required_outputs",
            _tuple_of_strings(tuple(self.required_outputs)),
        )
        object.__setattr__(
            self,
            "veto_rules",
            _tuple_of_strings(tuple(self.veto_rules)),
        )
        if not self.seat_id:
            raise ValueError("council seat definition id cannot be empty")
        if not self.role:
            raise ValueError("council seat definition role cannot be empty")
        if not self.mission:
            raise ValueError("council seat definition mission cannot be empty")
        if not self.required_outputs:
            raise ValueError("council seat definition required outputs cannot be empty")

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "seat_id": self.seat_id,
            "role": self.role,
            "mission": self.mission,
            "required_outputs": list(self.required_outputs),
            "veto_rules": list(self.veto_rules),
        }


@dataclass(frozen=True)
class CouncilSeatAssignment:
    seat_id: str
    provider: str
    model: str
    profile: str = ""
    enabled: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "seat_id", self.seat_id.strip())
        object.__setattr__(self, "provider", self.provider.strip())
        object.__setattr__(self, "model", self.model.strip())
        object.__setattr__(self, "profile", self.profile.strip())
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        if not self.seat_id:
            raise ValueError("council seat assignment id cannot be empty")
        if not self.provider:
            raise ValueError("council seat assignment provider cannot be empty")
        if not self.model:
            raise ValueError("council seat assignment model cannot be empty")

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "seat_id": self.seat_id,
            "provider": self.provider,
            "model": self.model,
            "profile": self.profile,
            "enabled": self.enabled,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class CouncilAssignmentDiversity:
    enabled_seats: int
    unique_models: int
    score: float
    is_single_model: bool

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "enabled_seats": self.enabled_seats,
            "unique_models": self.unique_models,
            "score": self.score,
            "is_single_model": self.is_single_model,
        }


@dataclass(frozen=True)
class CouncilConfig:
    seat_definitions: tuple[CouncilSeatDefinition, ...] = field(
        default_factory=lambda: default_council_seat_definitions()
    )
    assignments: tuple[CouncilSeatAssignment, ...] = ()
    required_seat_ids: tuple[str, ...] = ()
    mode: CouncilRunMode = "council"
    enabled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "seat_definitions", tuple(self.seat_definitions))
        object.__setattr__(self, "assignments", tuple(self.assignments))
        object.__setattr__(
            self,
            "required_seat_ids",
            _tuple_of_strings(tuple(self.required_seat_ids)),
        )
        if self.mode not in {"quick", "council", "debate"}:
            raise ValueError("council mode must be quick, council, or debate")
        _ensure_unique_definition_ids(self.seat_definitions)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "seat_definitions": [
                definition.to_json_dict() for definition in self.seat_definitions
            ],
            "assignments": [
                assignment.to_json_dict() for assignment in self.assignments
            ],
            "required_seat_ids": list(self.required_seat_ids),
        }


def default_council_seat_definitions() -> tuple[CouncilSeatDefinition, ...]:
    return (
        CouncilSeatDefinition(
            seat_id="contrarian",
            role="唱反调",
            mission="Find blockers, false assumptions, and reasons the proposal should not ship yet.",
            required_outputs=(
                "hard_blockers",
                "counterexamples",
                "minimum_required_fix",
            ),
            veto_rules=("unmitigated correctness, safety, or rollback risk",),
        ),
        CouncilSeatDefinition(
            seat_id="first_principles",
            role="第一性原理",
            mission="Reduce the proposal to goals, constraints, invariants, and simpler alternatives.",
            required_outputs=(
                "core_objective",
                "non_negotiable_constraints",
                "simpler_path",
            ),
            veto_rules=("solution does not satisfy the stated objective",),
        ),
        CouncilSeatDefinition(
            seat_id="expander",
            role="扩展思路",
            mission="Add useful adjacent options without letting the review drift from the requested scope.",
            required_outputs=(
                "missed_options",
                "future_extensions",
                "scope_guardrails",
            ),
        ),
        CouncilSeatDefinition(
            seat_id="outsider",
            role="局外人",
            mission="Review from the perspective of a capable user who does not know internal context.",
            required_outputs=(
                "confusing_assumptions",
                "operational_blind_spots",
                "plain_language_summary",
            ),
        ),
        CouncilSeatDefinition(
            seat_id="executor",
            role="执行者",
            mission="Turn the proposal into an implementation check: dependencies, order, tests, and handoff risk.",
            required_outputs=(
                "execution_plan",
                "test_obligations",
                "integration_risks",
            ),
            veto_rules=("proposal cannot be implemented or verified with available interfaces",),
        ),
    )


def default_council_config(
    *,
    provider: str,
    model: str,
    mode: CouncilRunMode = "council",
) -> CouncilConfig:
    definitions = default_council_seat_definitions()
    return CouncilConfig(
        mode=mode,
        seat_definitions=definitions,
        assignments=tuple(
            CouncilSeatAssignment(
                seat_id=definition.seat_id,
                provider=provider,
                model=model,
            )
            for definition in definitions
        ),
        required_seat_ids=tuple(definition.seat_id for definition in definitions),
    )


def build_council_seats(config: CouncilConfig) -> tuple[CouncilSeat, ...]:
    if not config.enabled:
        return ()
    definitions = {definition.seat_id: definition for definition in config.seat_definitions}
    enabled_assignments = tuple(
        assignment for assignment in config.assignments if assignment.enabled
    )
    _ensure_known_assignments(enabled_assignments, definitions)
    _ensure_required_assignments(config.required_seat_ids, enabled_assignments)
    return tuple(
        _seat_from_assignment(assignment, definitions[assignment.seat_id])
        for assignment in enabled_assignments
    )


def council_assignment_diversity(
    assignments: tuple[CouncilSeatAssignment, ...],
) -> CouncilAssignmentDiversity:
    enabled = tuple(assignment for assignment in assignments if assignment.enabled)
    model_keys = {
        (assignment.provider, assignment.model)
        for assignment in enabled
    }
    enabled_count = len(enabled)
    unique_count = len(model_keys)
    score = unique_count / enabled_count if enabled_count else 0.0
    return CouncilAssignmentDiversity(
        enabled_seats=enabled_count,
        unique_models=unique_count,
        score=round(score, 4),
        is_single_model=enabled_count > 0 and unique_count == 1,
    )


def _seat_from_assignment(
    assignment: CouncilSeatAssignment,
    definition: CouncilSeatDefinition,
) -> CouncilSeat:
    metadata = {
        "seat_definition_id": definition.seat_id,
        "mission": definition.mission,
        "required_outputs": definition.required_outputs,
        "veto_rules": definition.veto_rules,
        "assignment_provider_model": f"{assignment.provider}:{assignment.model}",
        **dict(assignment.metadata),
    }
    return CouncilSeat(
        seat_id=definition.seat_id,
        provider=assignment.provider,
        model=assignment.model,
        role=definition.role,
        profile=assignment.profile,
        metadata=metadata,
    )


def _ensure_unique_definition_ids(
    definitions: tuple[CouncilSeatDefinition, ...],
) -> None:
    seen: set[str] = set()
    for definition in definitions:
        if definition.seat_id in seen:
            raise ValueError(f"duplicate council seat definition: {definition.seat_id}")
        seen.add(definition.seat_id)


def _ensure_known_assignments(
    assignments: tuple[CouncilSeatAssignment, ...],
    definitions: dict[str, CouncilSeatDefinition],
) -> None:
    seen: set[str] = set()
    for assignment in assignments:
        if assignment.seat_id not in definitions:
            raise ValueError(f"unknown council seat assignment: {assignment.seat_id}")
        if assignment.seat_id in seen:
            raise ValueError(f"duplicate council seat assignment: {assignment.seat_id}")
        seen.add(assignment.seat_id)


def _ensure_required_assignments(
    required_seat_ids: tuple[str, ...],
    assignments: tuple[CouncilSeatAssignment, ...],
) -> None:
    assigned = {assignment.seat_id for assignment in assignments}
    for seat_id in required_seat_ids:
        if seat_id not in assigned:
            raise ValueError(f"required council seat is not assigned: {seat_id}")
