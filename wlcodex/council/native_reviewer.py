from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import re
from typing import Any, Protocol

from wlcodex.council.models import (
    CouncilReviewRequest,
    CouncilReviewResult,
    CouncilSeat,
)


class NativeProviderResolver(Protocol):
    def get(self, provider: str) -> Any: ...


class NativeProviderCouncilReviewer:
    def __init__(
        self,
        *,
        provider_resolver: NativeProviderResolver,
        default_cwd: str,
    ) -> None:
        self._provider_resolver = provider_resolver
        self._default_cwd = default_cwd

    async def review(self, request: CouncilReviewRequest) -> CouncilReviewResult:
        provider = self._provider_resolver.get(request.seat.provider)
        status = await provider.status()
        if not bool(getattr(status, "connected", False)):
            message = str(getattr(status, "message", "") or "").strip()
            error = f"{request.seat.provider} provider is not connected"
            if message:
                error = f"{error}: {message}"
            return CouncilReviewResult.failed(request.seat, error)

        prompt = _review_prompt(request)
        result = await provider.start_session(
            self._default_cwd,
            prompt,
            model=request.seat.model,
        )
        native_session_id = str(getattr(result, "native_session_id", "") or "")
        provider_engine = str(getattr(result, "provider_engine", "") or "")
        review_text = await _read_review_text(provider, native_session_id)
        parsed = _parse_review_payload(review_text)
        if parsed is None:
            return CouncilReviewResult.started(
                request.seat,
                native_session_id=native_session_id,
                provider_engine=provider_engine,
            )
        return _result_from_payload(
            request.seat,
            payload=parsed,
            raw_output=review_text,
        )


def _review_prompt(request: CouncilReviewRequest) -> str:
    seat = request.seat
    return "\n".join(
        [
            "# LLM Council Seat Review",
            f"seat_id: {seat.seat_id}",
            f"provider: {seat.provider}",
            f"model: {seat.model}",
            f"role: {seat.role}",
            f"packet_fingerprint: {request.packet.fingerprint}",
            *_seat_protocol_lines(seat),
            "",
            request.packet.render_for_review(),
            "",
            "## Hard Rules",
            "- Review only; do not edit files.",
            "- Judge the proposal as written.",
            "- Return exactly one JSON object.",
            "",
            "## JSON Schema",
            json.dumps(
                {
                    "verdict": "approve | approve_with_changes | reject | need_more_info",
                    "confidence": 0.0,
                    "summary": "one concise paragraph",
                    "risks": ["risk 1"],
                    "required_changes": ["change 1"],
                    "open_questions": ["question 1"],
                },
                ensure_ascii=False,
                indent=2,
            ),
        ]
    )


def _seat_protocol_lines(seat: CouncilSeat) -> list[str]:
    mission = str(seat.metadata.get("mission") or "").strip()
    required_outputs = _strings(seat.metadata.get("required_outputs"))
    veto_rules = _strings(seat.metadata.get("veto_rules"))
    if not (seat.profile or mission or required_outputs or veto_rules):
        return []

    lines = ["", "## Seat Protocol"]
    if seat.profile:
        lines.extend(["profile:", seat.profile])
    if mission:
        lines.extend(["mission:", mission])
    if required_outputs:
        lines.append("required_outputs:")
        lines.extend(f"- {item}" for item in required_outputs)
    if veto_rules:
        lines.append("veto_rules:")
        lines.extend(f"- {item}" for item in veto_rules)
    return lines


async def _read_review_text(provider: Any, native_session_id: str) -> str:
    if not native_session_id:
        return ""
    try:
        session = await provider.read_session(native_session_id)
    except Exception:
        return ""
    return _collect_text(session)


def _collect_text(value: Any) -> str:
    pieces: list[str] = []

    def walk(item: Any) -> None:
        if isinstance(item, str):
            pieces.append(item)
            return
        if isinstance(item, Mapping):
            for key in ("text", "content", "output", "delta"):
                text = item.get(key)
                if isinstance(text, str):
                    pieces.append(text)
            for child in item.values():
                if isinstance(child, (Mapping, Sequence)) and not isinstance(child, str):
                    walk(child)
            return
        if isinstance(item, Sequence) and not isinstance(item, str):
            for child in item:
                walk(child)

    walk(value)
    return "\n".join(piece for piece in pieces if piece.strip())


def _parse_review_payload(text: str) -> dict[str, Any] | None:
    for candidate in _json_candidates(text):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("verdict"):
            return payload
    return None


def _json_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    candidates.extend(
        match.group(1).strip()
        for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    )
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    return candidates


def _result_from_payload(
    seat: CouncilSeat,
    *,
    payload: dict[str, Any],
    raw_output: str,
) -> CouncilReviewResult:
    return CouncilReviewResult(
        seat_id=seat.seat_id,
        provider=seat.provider,
        model=seat.model,
        verdict=str(payload.get("verdict") or "need_more_info"),
        confidence=_float(payload.get("confidence")),
        summary=str(payload.get("summary") or ""),
        risks=_strings(payload.get("risks")),
        required_changes=_strings(payload.get("required_changes")),
        open_questions=_strings(payload.get("open_questions")),
        raw_output=raw_output,
    )


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
