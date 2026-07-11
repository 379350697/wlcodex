"""Read-only Relay conversation projection.

This module owns the user-visible conversation contract.  Its dependencies are
passed explicitly so it cannot read, reconcile, or dispatch Relay work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class RelayConversationProjectionDependencies:
    worker_events_for_roles: Callable[..., list[Any]]
    user_message_dedupe_text: Callable[[str], str]
    native_message_key: Callable[..., str]
    native_event_text: Callable[[Any], str]
    text_is_structured_artifact_placeholder: Callable[[str], bool]
    text_contains_relay_protocol_payload: Callable[[str], bool]
    parse_role_envelope_payload: Callable[[str], Any]
    native_event_row: Callable[[str, str, Any], dict[str, str] | None]
    project_native_conversation_row: Callable[..., dict[str, str] | None]
    sanitize_protocol_leak_text: Callable[[str, str], str]
    conversation_row_is_task_status_noise: Callable[[dict[str, Any]], bool]
    conversation_row_from_artifact: Callable[..., dict[str, str] | None]
    pending_followup_waiting_row: Callable[..., dict[str, str] | None]
    prune_direct_final_summary_rows: Callable[[list[dict[str, Any]]], None]
    normalize_conversation_lifecycle_rows: Callable[[list[dict[str, str]]], None]
    first_blocked_role: Callable[[list[Any]], str]
    current_round_id_from_artifacts: Callable[[Any], str]
    role_label: Callable[[str], str]


def project_relay_conversation_rows(
    role_jobs: list[Any],
    *,
    hub: Any | None,
    canonical_payloads: dict[str, dict[str, Any]] | None = None,
    canonical_payload_sequence: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    deps: RelayConversationProjectionDependencies,
) -> list[dict[str, str]]:
    """Project persisted Relay artifacts and live events into display rows.

    ``canonical_payloads`` and ``canonical_payload_sequence`` remain accepted
    for the stable public contract; the row reducer intentionally derives its
    truth from artifacts and live events only.
    """

    del canonical_payloads, canonical_payload_sequence
    events = deps.worker_events_for_roles(role_jobs, hub=hub)
    job_by_role = {str(getattr(job, "role", "") or ""): job for job in role_jobs}
    user_followup_texts = {
        deps.user_message_dedupe_text(str(artifact.get("text") or ""))
        for artifact in (artifacts or [])
        if str(artifact.get("artifact_type") or "") == "user_followup"
        and deps.user_message_dedupe_text(str(artifact.get("text") or ""))
    }
    completed_keys = {
        deps.native_message_key(role, worker_event, bucket="assistant")
        for _occurred_at, _event_id, role, _display_name, worker_event in events
        if worker_event.kind == "message_completed"
        and deps.native_event_text(worker_event).strip()
    }
    rows: list[dict[str, str]] = []
    row_by_key: dict[str, dict[str, str]] = {}
    for _occurred_at, _event_id, role, display_name, worker_event in events:
        kind = str(worker_event.kind or "event")
        text = deps.native_event_text(worker_event)
        if kind in {"text_delta", "message_completed"} and (
            deps.text_is_structured_artifact_placeholder(text)
            or deps.text_contains_relay_protocol_payload(text)
            or deps.parse_role_envelope_payload(text) is not None
        ):
            continue
        if kind == "text_delta":
            key = deps.native_message_key(role, worker_event, bucket="assistant")
            if key in completed_keys:
                continue
            if key not in row_by_key:
                row = {
                    "role": role,
                    "kind": kind,
                    "speaker": display_name,
                    "meta": str(worker_event.source or ""),
                    "body": "",
                    "key": key,
                    "round_id": "",
                    "preview_event_ids": str(worker_event.id),
                }
                rows.append(row)
                row_by_key[key] = row
            event_ids = set(filter(None, row_by_key[key].get("preview_event_ids", "").split(",")))
            event_ids.add(str(worker_event.id))
            row_by_key[key]["preview_event_ids"] = ",".join(sorted(event_ids, key=int))
            row_by_key[key]["body"] += text
            continue
        row = deps.native_event_row(role, display_name, worker_event)
        if row is not None:
            rows.append(row)

    projected_rows: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    seen_user_bodies: set[str] = set()
    for row in rows:
        projected = deps.project_native_conversation_row(
            row,
            job=job_by_role.get(str(row.get("role") or "")),
        )
        if projected is None and str(row.get("kind") or "") == "message_completed":
            role = str(row.get("role") or "")
            body = deps.sanitize_protocol_leak_text(role, str(row.get("body") or ""))
            if (
                body
                and not deps.text_is_structured_artifact_placeholder(body)
                and not deps.text_contains_relay_protocol_payload(body)
                and deps.parse_role_envelope_payload(body) is None
                and not deps.conversation_row_is_task_status_noise(
                    {"kind": "message_completed", "body": body}
                )
            ):
                projected = {**row, "body": body}
        if projected is None or deps.conversation_row_is_task_status_noise(projected):
            continue
        if str(projected.get("kind") or "") == "user_message":
            body = str(projected.get("body") or "").strip()
            dedupe_body = deps.user_message_dedupe_text(body)
            if not dedupe_body or dedupe_body in user_followup_texts:
                continue
            if dedupe_body in seen_user_bodies:
                continue
            seen_user_bodies.add(dedupe_body)
        key = str(projected.get("key") or "")
        if key and key in seen_keys:
            continue
        if key:
            seen_keys.add(key)
        projected_rows.append(projected)

    if artifacts is not None:
        for index, artifact in enumerate(artifacts):
            artifact_row = deps.conversation_row_from_artifact(
                artifact,
                index=index,
                job_by_role=job_by_role,
                user_followup_texts=user_followup_texts,
            )
            if artifact_row is None:
                continue
            if str(artifact_row.get("kind") or "") == "user_message":
                body = str(artifact_row.get("body") or "").strip()
                dedupe_body = deps.user_message_dedupe_text(body)
                if not dedupe_body or dedupe_body in seen_user_bodies:
                    continue
                seen_user_bodies.add(dedupe_body)
            key = str(artifact_row.get("key") or "")
            if key and key in seen_keys:
                continue
            if key:
                seen_keys.add(key)
            projected_rows.append(artifact_row)
        pending_row = deps.pending_followup_waiting_row(artifacts, job_by_role)
        if pending_row is not None:
            key = str(pending_row.get("key") or "")
            if not key or key not in seen_keys:
                if key:
                    seen_keys.add(key)
                projected_rows.append(pending_row)

    deps.prune_direct_final_summary_rows(projected_rows)
    deps.normalize_conversation_lifecycle_rows(projected_rows)
    has_pending_followup_waiting = any(
        str(row.get("kind") or "") == "waiting"
        and str(row.get("key") or "").startswith("followup-waiting:")
        for row in projected_rows
    )
    blocked_role = deps.first_blocked_role(role_jobs)
    current_round_id = deps.current_round_id_from_artifacts(artifacts)
    has_current_round_blocked_role_result = any(
        str(row.get("role") or "") == blocked_role
        and str(row.get("kind") or "") in {"role_envelope", "followup_response", "role_process"}
        and (
            str(row.get("kind") or "") == "followup_response"
            or str(row.get("artifact_type") or "") == "final_summary"
        )
        and str(row.get("round_id") or current_round_id) == current_round_id
        for row in projected_rows
    )
    if blocked_role and not has_pending_followup_waiting and not has_current_round_blocked_role_result:
        projected_rows.append(
            {
                "role": blocked_role,
                "kind": "status",
                "speaker": "系统",
                "meta": "",
                "body": f"接力暂停在{deps.role_label(blocked_role)}，详情见工作日志。",
                "key": f"relay-paused:{blocked_role}",
            }
        )
    return projected_rows
