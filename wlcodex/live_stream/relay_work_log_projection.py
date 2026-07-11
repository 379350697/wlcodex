"""Read-only work-log projection composition for Relay task details."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


def render_relay_work_log_body(
    detail: Any,
    *,
    hub: Any | None,
    canonical_payloads: dict[str, dict[str, Any]] | None,
    build_segments: Callable[..., list[Any]],
    render_segment: Callable[..., str],
    render_empty: Callable[[], str],
) -> str:
    """Render an already-read task detail without touching Relay lifecycle."""

    segments = build_segments(
        detail,
        hub=hub,
        canonical_payloads=canonical_payloads,
    )
    rows = [
        render_segment(segment, index=index)
        for index, segment in enumerate(segments)
    ]
    return "\n".join(rows) if rows else render_empty()


@dataclass(frozen=True)
class RelayWorkLogProjectionDependencies:
    role_ids: frozenset[str]
    role_errors: Callable[[Any], dict[str, dict[str, Any]]]
    invalid_artifacts: Callable[[Any], dict[str, dict[str, Any]]]
    artifact_payloads: Callable[[Any], dict[str, dict[str, Any]]]
    worker_events: Callable[..., list[Any]]
    native_message_key: Callable[..., str]
    native_event_text: Callable[[Any], str]
    text_is_protocol_noise: Callable[[str], bool]
    public_role: Callable[[str], tuple[str, str]]
    segment_factory: Callable[..., Any]
    entry_factory: Callable[..., Any]
    merge_entry: Callable[[Any, Any], None]
    entry_from_event: Callable[[str, Any], Any | None]
    humanize_role_envelope: Callable[[dict[str, Any]], str]
    action_label: Callable[[str, dict[str, Any]], str]
    role_status_label: Callable[[str], str]
    confirmation_source_label: Callable[[str, str], str]
    finalize_segments: Callable[[list[Any]], None]
    payload_status_is_success: Callable[[str], bool]
    role_label: Callable[[str], str]
    humanize_display_text: Callable[[str], str]


def build_relay_work_log_segments(
    detail: Any,
    *,
    hub: Any | None,
    canonical_payloads: dict[str, dict[str, Any]] | None,
    deps: RelayWorkLogProjectionDependencies,
) -> list[Any]:
    """Reduce persisted artifacts and live events to work-log view models."""

    canonical_payloads = canonical_payloads or {}
    artifacts = getattr(detail, "artifacts", []) or []
    role_errors = deps.role_errors(artifacts)
    invalid_artifacts = deps.invalid_artifacts(artifacts)
    artifact_payloads = deps.artifact_payloads(artifacts)
    events = deps.worker_events(detail.role_jobs, hub=hub)
    protocol_completed_keys = {
        deps.native_message_key(role, worker_event, bucket="assistant")
        for _occurred_at, _event_id, role, _display_name, worker_event in events
        if worker_event.kind == "message_completed"
        and deps.text_is_protocol_noise(deps.native_event_text(worker_event))
    }
    segments: list[Any] = []
    entry_maps: list[dict[str, Any]] = []

    def append_segment(role: str) -> Any:
        persona, display_name = deps.public_role(role)
        segment = deps.segment_factory(
            role=role,
            persona=persona,
            display_name=display_name,
            entries=[],
        )
        segments.append(segment)
        entry_maps.append({})
        return segment

    def append_entry(role: str, entry: Any) -> None:
        if not role or role not in deps.role_ids:
            return
        segment = segments[-1] if segments and segments[-1].role == role else append_segment(role)
        key_map = entry_maps[-1]
        existing = key_map.get(entry.key) if entry.key else None
        if existing is None:
            segment.entries.append(entry)
            if entry.key:
                key_map[entry.key] = entry
            return
        deps.merge_entry(existing, entry)

    for _occurred_at, _event_id, role, _display_name, worker_event in events:
        if (
            worker_event.kind == "text_delta"
            and deps.native_message_key(role, worker_event, bucket="assistant")
            in protocol_completed_keys
        ):
            continue
        entry = deps.entry_from_event(role, worker_event)
        if entry is not None:
            append_entry(role, entry)

    artifact_keys_added: set[str] = set()
    for segment in segments:
        payload = canonical_payloads.get(segment.role) or artifact_payloads.get(segment.role)
        fallback_payload = artifact_payloads.get(segment.role)
        if (
            payload is not None
            and fallback_payload is not None
            and deps.text_is_protocol_noise(str(payload.get("summary") or payload.get("output") or ""))
        ):
            payload = fallback_payload
        if payload is None:
            continue
        key = f"artifact:{segment.role}"
        if key in artifact_keys_added:
            continue
        segment.entries.append(
            deps.entry_factory(
                kind="artifact",
                key=key,
                text=deps.humanize_role_envelope(payload),
                chip=(
                    f"{deps.action_label(segment.role, payload)} "
                    f"{deps.role_status_label(str(payload.get('status') or 'passed'))}"
                ),
            )
        )
        artifact_keys_added.add(key)

    round_execution = getattr(detail, "round_execution", {}) or {}
    confirmation = round_execution.get("confirmation") if isinstance(round_execution, dict) else {}
    if not isinstance(confirmation, dict):
        confirmation = {}
    confirmation_source = str(confirmation.get("source") or "").strip()
    if (
        str(getattr(getattr(detail, "task", None), "status", "") or "") == "waiting_user"
        and confirmation_source
    ):
        role = str(confirmation.get("role") or "").strip()
        if role not in deps.role_ids:
            role = next(
                (
                    str(getattr(job, "role", "") or "")
                    for job in getattr(detail, "role_jobs", []) or []
                    if str(getattr(job, "status", "") or "") == "waiting"
                ),
                "director",
            )
        label = deps.confirmation_source_label(
            confirmation_source,
            str(confirmation.get("provider") or ""),
        ) or "等待确认"
        kind = str(confirmation.get("kind") or "relay_question")
        provider_request_id = str(confirmation.get("provider_request_id") or "")
        waiting_reason = str(round_execution.get("waiting_reason") or "")
        text_parts = [f"来源：{label}", f"请求类型：{kind}"]
        if waiting_reason:
            text_parts.append(f"等待原因：{waiting_reason}")
        if provider_request_id:
            text_parts.append(f"请求 ID：{provider_request_id}")
        append_entry(
            role,
            deps.entry_factory(
                kind="confirmation",
                key=(
                    f"confirmation:{getattr(detail, 'current_round_id', '')}:{role}:"
                    f"{confirmation_source}:{provider_request_id}"
                ),
                text="\n".join(text_parts),
                chip=label,
            ),
        )

    deps.finalize_segments(segments)
    existing_roles = {segment.role for segment in segments}
    for job in detail.role_jobs:
        role = str(getattr(job, "role", "") or "")
        if role not in deps.role_ids:
            continue
        role_success_payload = canonical_payloads.get(role) or artifact_payloads.get(role)
        has_lifecycle_success = deps.payload_status_is_success(
            str((role_success_payload or {}).get("status") or "")
        )
        if role in existing_roles or f"artifact:{role}" in artifact_keys_added:
            payload = None
        else:
            payload = role_success_payload
            fallback_payload = artifact_payloads.get(role)
            if (
                payload is not None
                and fallback_payload is not None
                and deps.text_is_protocol_noise(
                    str(payload.get("summary") or payload.get("output") or "")
                )
            ):
                payload = fallback_payload
        role_error = role_errors.get(role) or {}
        error_message = str(
            getattr(job, "error_message", "")
            or role_error.get("error")
            or role_error.get("summary")
            or role_error.get("message")
            or ""
        ).strip()
        status = str(getattr(job, "status", "") or "")
        if payload is not None:
            append_entry(
                role,
                deps.entry_factory(
                    kind="artifact",
                    key=f"artifact:{role}",
                    text=deps.humanize_role_envelope(payload),
                    chip=(
                        f"{deps.action_label(role, payload)} "
                        f"{deps.role_status_label(str(payload.get('status') or status or 'passed'))}"
                    ),
                ),
            )
            existing_roles.add(role)
        if error_message and (role_error or not has_lifecycle_success):
            append_entry(
                role,
                deps.entry_factory(
                    kind="error",
                    key=f"error:{role}",
                    text=f"{deps.role_label(role)}执行问题：{deps.humanize_display_text(error_message)}",
                    chip="调用失败",
                    failed=True,
                ),
            )
            existing_roles.add(role)
        invalid_payload = invalid_artifacts.get(role) or {}
        if invalid_payload:
            append_entry(
                role,
                deps.entry_factory(
                    kind="artifact_invalid",
                    key=f"artifact_invalid:{invalid_payload.get('id') or role}",
                    text="结构化产物未采用，自动流转暂停。已保留 provider 原始可见输出。",
                    chip="等待修正",
                    output=str(invalid_payload.get("error") or ""),
                ),
            )
            existing_roles.add(role)
        if role not in existing_roles and status and status not in {"idle", "passed", "completed"}:
            _persona, display_name = deps.public_role(role)
            append_entry(
                role,
                deps.entry_factory(
                    kind="status",
                    key=f"status:{role}:{status}",
                    text=f"{display_name} {deps.role_status_label(status)}",
                ),
            )
            existing_roles.add(role)

    deps.finalize_segments(segments)
    return segments
