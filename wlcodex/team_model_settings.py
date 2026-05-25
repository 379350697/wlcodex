from __future__ import annotations

import json
from collections.abc import Mapping

from wlcodex.status import TEAM_ROLE_LABELS, TEAM_ROLE_ORDER

RUNTIME_TEAM_MODEL_ASSIGNMENT_PREFIX = "adaptive_team.assignment."

TEAM_MODEL_PROFILE_ORDER: tuple[str, ...] = (
    "codex_gpt",
    "claude_deepseek",
)

TEAM_MODEL_MULTI_SELECT_ROLES: frozenset[str] = frozenset({"implementer"})


def runtime_assignment_key(role_id: str) -> str:
    return f"{RUNTIME_TEAM_MODEL_ASSIGNMENT_PREFIX}{role_id}"


def ordered_team_roles(assignments: Mapping[str, tuple[str, ...]]) -> tuple[str, ...]:
    known = [role for role in TEAM_ROLE_ORDER if role in assignments]
    extra = sorted(role for role in assignments if role not in TEAM_ROLE_ORDER)
    return tuple(known + extra)


def ordered_model_profiles(model_profiles: Mapping[str, str]) -> tuple[str, ...]:
    known = [profile for profile in TEAM_MODEL_PROFILE_ORDER if profile in model_profiles]
    extra = sorted(profile for profile in model_profiles if profile not in TEAM_MODEL_PROFILE_ORDER)
    return tuple(known + extra)


def is_multi_select_role(role_id: str) -> bool:
    return role_id in TEAM_MODEL_MULTI_SELECT_ROLES


def role_display_name(role_id: str) -> str:
    return TEAM_ROLE_LABELS.get(role_id, role_id)


def normalize_assignment(
    role_id: str,
    profiles: tuple[str, ...] | list[str] | None,
    available_profiles: tuple[str, ...],
    fallback: tuple[str, ...],
) -> tuple[str, ...]:
    available = set(available_profiles)
    normalized: list[str] = []
    for profile in profiles or ():
        value = str(profile).strip()
        if value and value in available and value not in normalized:
            normalized.append(value)

    if not normalized:
        normalized = [
            profile for profile in fallback if profile in available and profile not in normalized
        ]

    if not normalized and available_profiles:
        normalized = [available_profiles[0]]

    if not is_multi_select_role(role_id):
        normalized = normalized[:1]

    return tuple(normalized)


def encode_assignment(profiles: tuple[str, ...]) -> str:
    return json.dumps(list(profiles), ensure_ascii=False)


def decode_assignment(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, list):
        return None
    return tuple(str(item) for item in decoded if str(item).strip())


def load_runtime_assignments(
    ledger: object | None,
    defaults: Mapping[str, tuple[str, ...]],
    model_profiles: Mapping[str, str],
) -> dict[str, tuple[str, ...]]:
    available_profiles = ordered_model_profiles(model_profiles)
    assignments = {
        str(role): tuple(str(profile) for profile in profiles)
        for role, profiles in defaults.items()
    }
    if ledger is None or not hasattr(ledger, "get_runtime_setting"):
        return assignments

    for role_id in ordered_team_roles(assignments):
        raw = ledger.get_runtime_setting(runtime_assignment_key(role_id))
        decoded = decode_assignment(raw)
        if decoded is None:
            continue
        assignments[role_id] = normalize_assignment(
            role_id,
            decoded,
            available_profiles,
            assignments.get(role_id, ()),
        )
    return assignments
