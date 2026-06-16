from __future__ import annotations

import json
import re
from typing import Any

from wlcodex.relay.models import (
    RELAY_ARTIFACT_TYPES,
    RELAY_ROLE_IDS,
    EnvelopeParseResult,
    RoleEnvelope,
)


_REQUIRED_FIELDS = (
    "status",
    "reason",
    "role",
    "artifact_type",
    "handoff_to",
    "summary",
    "evidence_refs",
    "open_questions",
    "next_action",
)
_DEFAULT_HANDOFFS = {
    "architect": "implementer",
    "implementer": "tester",
    "tester": "auditor",
    "auditor": "director",
}
_VALID_ENVELOPE_STATUSES = {"passed", "failed", "blocked", "waiting"}


def default_handoff_target(role: str) -> str | None:
    return _DEFAULT_HANDOFFS.get(role)


def parse_role_envelope(text: str | dict[str, Any]) -> EnvelopeParseResult:
    payloads: list[Any] = [text]
    direct_json_error = ""
    if isinstance(text, str):
        try:
            payloads = [json.loads(text)]
        except json.JSONDecodeError as exc:
            direct_json_error = f"invalid json: {exc.msg}"
            payloads = _extract_json_payloads(text)
            if not payloads:
                return EnvelopeParseResult(ok=False, error=direct_json_error)
    last_error = direct_json_error
    for payload in payloads:
        result = _parse_role_envelope_payload(payload)
        if result.ok:
            return result
        last_error = result.error or last_error
    return EnvelopeParseResult(
        ok=False,
        error=last_error or "role envelope must be a JSON object",
    )


def _parse_role_envelope_payload(payload: Any) -> EnvelopeParseResult:
    if not isinstance(payload, dict):
        return EnvelopeParseResult(ok=False, error="role envelope must be a JSON object")
    if isinstance(payload.get("role_envelope"), dict):
        payload = payload["role_envelope"]

    missing = [field for field in _REQUIRED_FIELDS if field not in payload]
    if missing:
        return EnvelopeParseResult(
            ok=False,
            error=f"missing required fields: {', '.join(missing)}",
        )

    envelope = RoleEnvelope.from_payload(payload)
    if envelope.role not in RELAY_ROLE_IDS:
        return EnvelopeParseResult(ok=False, error=f"unknown role: {envelope.role}")
    if envelope.status not in _VALID_ENVELOPE_STATUSES:
        return EnvelopeParseResult(ok=False, error=f"invalid status: {envelope.status}")
    if envelope.artifact_type not in RELAY_ARTIFACT_TYPES:
        return EnvelopeParseResult(
            ok=False,
            error=f"invalid artifact_type: {envelope.artifact_type}",
        )
    if envelope.handoff_to and envelope.handoff_to not in RELAY_ROLE_IDS:
        return EnvelopeParseResult(
            ok=False,
            error=f"unknown handoff_to role: {envelope.handoff_to}",
        )
    if not isinstance(payload.get("evidence_refs"), list) or not isinstance(
        payload.get("open_questions"), list
    ):
        return EnvelopeParseResult(
            ok=False,
            error="evidence_refs and open_questions must be lists",
        )

    next_role = envelope.handoff_to or default_handoff_target(envelope.role)
    return EnvelopeParseResult(
        ok=True,
        envelope=envelope,
        next_role=next_role,
        payload=dict(payload),
    )


def _extract_json_payloads(text: str) -> list[Any]:
    payloads: list[Any] = []
    for match in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.DOTALL):
        payload = _load_json_payload(match.group(1).strip())
        if payload is not None:
            payloads.append(payload)

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if payload not in payloads:
            payloads.append(payload)
    return payloads


def _load_json_payload(text: str) -> Any | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
