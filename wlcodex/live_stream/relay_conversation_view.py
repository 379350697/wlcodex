"""Relay conversation assembly independent of task and persistence models."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def render_conversation_rows(
    rows: list[dict[str, Any]],
    *,
    current_round_id: str,
    empty_html: str,
    waiting_html: str,
    render_handoff: Callable[[dict[str, Any]], str],
    render_message: Callable[[dict[str, Any]], str],
    handoff_pair: Callable[[dict[str, Any]], tuple[str, str] | None],
    handoff_identity: Callable[[dict[str, Any]], tuple[str, str, str, str]],
) -> str:
    if not rows:
        return waiting_html or empty_html

    def is_current_round_row(row: dict[str, Any]) -> bool:
        round_id = str(row.get("round_id") or "").strip()
        return not round_id or round_id == current_round_id

    html_rows: list[str] = []
    previous_role = ""
    previous_round_id = ""
    handoffs_by_role: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if str(row.get("kind") or "") != "handoff" or not is_current_round_row(row):
            continue
        to_role = str(row.get("to_role") or row.get("role") or "")
        if to_role:
            handoffs_by_role.setdefault(to_role, []).append(row)
    rendered_handoffs: set[str] = set()
    rendered_handoff_identities: set[tuple[str, str, str, str]] = set()

    def append_handoff_once(handoff: dict[str, Any]) -> bool:
        if not is_current_round_row(handoff):
            return False
        key = str(handoff.get("key") or "")
        if handoff_pair(handoff) is None:
            return False
        identity = handoff_identity(handoff)
        if identity in rendered_handoff_identities or (key and key in rendered_handoffs):
            return True
        html = render_handoff(handoff)
        if not html:
            return False
        html_rows.append(html)
        rendered_handoff_identities.add(identity)
        if key:
            rendered_handoffs.add(key)
        return True

    for row in rows:
        role = str(row.get("role") or "")
        kind = str(row.get("kind") or "")
        row_round_id = str(row.get("round_id") or current_round_id)
        if row_round_id != previous_round_id:
            previous_role = ""
            previous_round_id = row_round_id
        if kind == "handoff":
            if is_current_round_row(row):
                append_handoff_once(row)
            continue
        if kind == "user_message":
            html_rows.append(render_message(row))
            previous_role = ""
            continue
        if kind == "role_process" and not is_current_round_row(row):
            continue
        if role:
            for handoff in handoffs_by_role.get(role, []):
                append_handoff_once(handoff)
        if kind in {"role_envelope", "role_process"} and previous_role and role and role != previous_role:
            for handoff in handoffs_by_role.get(role, []):
                if handoff_pair(handoff) == (previous_role, role):
                    append_handoff_once(handoff)
        if (
            kind in {"role_envelope", "role_process"}
            and is_current_round_row(row)
            and previous_role == "director"
            and role
            and role != "director"
            and handoff_identity({**row, "from_role": previous_role, "to_role": role})
            not in rendered_handoff_identities
        ):
            append_handoff_once(
                {
                    "from_role": previous_role,
                    "to_role": role,
                    "role": role,
                    "round_id": str(row.get("round_id") or ""),
                    "key": f"synthetic-handoff:{previous_role}:{role}",
                }
            )
        html_rows.append(render_message(row))
        if kind in {"role_envelope", "role_process"}:
            previous_role = role
    return "\n".join(html_rows)
