from __future__ import annotations

from typing import Any

import pytest

from wlcodex.council import CouncilReviewPacket, CouncilReviewRequest, CouncilSeat
from wlcodex.council.native_reviewer import NativeProviderCouncilReviewer
from wlcodex.native_agents.models import (
    NativeAgentCapabilities,
    NativeAgentControlResult,
    NativeAgentStatus,
)


class FakeProviderResolver:
    def __init__(self, providers: dict[str, object]) -> None:
        self._providers = providers

    def get(self, provider: str) -> object:
        return self._providers[provider]


class FakeNativeProvider:
    provider = "codex"
    provider_engine = "app-server"

    def __init__(
        self,
        *,
        connected: bool = True,
        session_payload: dict[str, Any] | None = None,
    ) -> None:
        self.connected = connected
        self.session_payload = session_payload if session_payload is not None else {}
        self.start_calls: list[dict[str, Any]] = []

    async def status(self) -> NativeAgentStatus:
        return NativeAgentStatus(
            provider=self.provider,
            provider_engine=self.provider_engine,
            enabled=True,
            connected=self.connected,
            status_code="ok" if self.connected else "offline",
            message="" if self.connected else "not connected",
        )

    def capabilities(self) -> NativeAgentCapabilities:
        return NativeAgentCapabilities(can_start_session=True, can_read_history=True)

    async def start_session(
        self,
        cwd: str,
        prompt: str,
        **kwargs: Any,
    ) -> NativeAgentControlResult:
        self.start_calls.append({"cwd": cwd, "prompt": prompt, "kwargs": kwargs})
        return NativeAgentControlResult(
            provider=self.provider,
            provider_engine=self.provider_engine,
            native_session_id="native-review-1",
            agent_run_id=42,
            status="started",
        )

    async def read_session(self, native_session_id: str) -> dict[str, Any]:
        assert native_session_id == "native-review-1"
        return self.session_payload


def _packet() -> CouncilReviewPacket:
    return CouncilReviewPacket(
        title="Provider Adapter",
        proposal="Call an existing native provider through a Council reviewer adapter.",
        constraints=("Only review the proposal; do not edit files.",),
    )


def _seat() -> CouncilSeat:
    return CouncilSeat(
        seat_id="codex-gpt55",
        provider="codex",
        model="gpt-5.5",
        role="implementation reviewer",
        profile="Prefer concrete integration concerns over generic praise.",
        metadata={
            "mission": "Verify implementation order, dependencies, and test obligations.",
            "required_outputs": (
                "execution_plan",
                "test_obligations",
                "integration_risks",
            ),
            "veto_rules": ("proposal cannot be verified with available interfaces",),
        },
    )


@pytest.mark.asyncio
async def test_native_reviewer_starts_provider_and_parses_structured_review() -> None:
    provider = FakeNativeProvider(
        session_payload={
            "turns": [
                {
                    "role": "assistant",
                    "text": (
                        "```json\n"
                        '{"verdict":"reject","confidence":0.61,'
                        '"summary":"Adapter needs a stricter boundary.",'
                        '"risks":["provider can edit files"],'
                        '"required_changes":["force read-only instruction"],'
                        '"open_questions":["which seats are enabled?"]}'
                        "\n```"
                    ),
                }
            ]
        }
    )
    reviewer = NativeProviderCouncilReviewer(
        provider_resolver=FakeProviderResolver({"codex": provider}),
        default_cwd="/repo",
    )

    result = await reviewer.review(CouncilReviewRequest(packet=_packet(), seat=_seat()))

    assert result.status == "completed"
    assert result.seat_id == "codex-gpt55"
    assert result.provider == "codex"
    assert result.model == "gpt-5.5"
    assert result.verdict == "reject"
    assert result.confidence == 0.61
    assert result.summary == "Adapter needs a stricter boundary."
    assert result.risks == ("provider can edit files",)
    assert result.required_changes == ("force read-only instruction",)
    assert provider.start_calls[0]["cwd"] == "/repo"
    assert provider.start_calls[0]["kwargs"]["model"] == "gpt-5.5"
    assert _packet().fingerprint in provider.start_calls[0]["prompt"]
    assert "Only review the proposal; do not edit files." in provider.start_calls[0]["prompt"]
    assert "Verify implementation order, dependencies, and test obligations." in (
        provider.start_calls[0]["prompt"]
    )
    assert "execution_plan" in provider.start_calls[0]["prompt"]
    assert "proposal cannot be verified with available interfaces" in (
        provider.start_calls[0]["prompt"]
    )


@pytest.mark.asyncio
async def test_native_reviewer_returns_started_when_history_has_no_review_text() -> None:
    provider = FakeNativeProvider(session_payload={"turns": []})
    reviewer = NativeProviderCouncilReviewer(
        provider_resolver=FakeProviderResolver({"codex": provider}),
        default_cwd="/repo",
    )

    result = await reviewer.review(CouncilReviewRequest(packet=_packet(), seat=_seat()))

    assert result.status == "started"
    assert result.verdict == "need_more_info"
    assert result.confidence == 0.0
    assert result.summary == "Native review session started: native-review-1"


@pytest.mark.asyncio
async def test_native_reviewer_fails_before_start_when_provider_is_disconnected() -> None:
    provider = FakeNativeProvider(connected=False)
    reviewer = NativeProviderCouncilReviewer(
        provider_resolver=FakeProviderResolver({"codex": provider}),
        default_cwd="/repo",
    )

    result = await reviewer.review(CouncilReviewRequest(packet=_packet(), seat=_seat()))

    assert result.status == "failed"
    assert result.error == "codex provider is not connected: not connected"
    assert provider.start_calls == []
