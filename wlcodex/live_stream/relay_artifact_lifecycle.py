"""Shared, read-only lifecycle interpretation for Relay artifacts.

Persisted artifacts are read by task detail, conversation, and work-log
surfaces.  Their historical status values are not always directly
user-presentable, so this module is the one contract for interpreting them.
It deliberately performs no database or provider work.
"""

from __future__ import annotations

from typing import Any


def payload_status_is_success(status: str) -> bool:
    return str(status or "").strip() in {"passed", "completed", "success", "succeeded", "done"}


def payload_status_is_terminal_failure(status: str) -> bool:
    return str(status or "").strip() in {"failed", "blocked", "error", "interrupted"}


def lifecycle_status_for_payload(
    payload: dict[str, Any],
    success_roles_in_round: set[str] | None = None,
) -> str:
    """Return the display lifecycle state for one persisted artifact.

    Older director final-summary rows can remain ``waiting`` after the
    independent auditor has completed.  Treat that persisted legacy shape as
    successful only at read time; this must never mutate the artifact.
    """

    status = str(payload.get("status") or "").strip()
    role = str(payload.get("role") or payload.get("relay_role") or "").strip()
    artifact_type = str(payload.get("artifact_type") or "").strip()
    handoff_to = str(payload.get("handoff_to") or "").strip()
    if (
        role == "director"
        and artifact_type == "final_summary"
        and status == "waiting"
        and not handoff_to
        and "auditor" in (success_roles_in_round or set())
    ):
        return "passed"
    if not status and artifact_type in {"followup_response", "final_summary"} and not handoff_to:
        return "passed"
    return status or "passed"
