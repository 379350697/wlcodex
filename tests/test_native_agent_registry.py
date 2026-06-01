import pytest

from wlcodex.native_agents.models import NativeAgentCapabilities, NativeAgentStatus
from wlcodex.native_agents.provider import NativeAgentRegistry


class FakeProvider:
    provider = "codex"
    provider_engine = "app-server"

    async def status(self):
        return NativeAgentStatus(
            provider="codex",
            provider_engine="app-server",
            enabled=True,
            connected=True,
            status_code="ok",
        )

    def capabilities(self):
        return NativeAgentCapabilities(can_list_sessions=True)


def test_registry_returns_provider_by_name() -> None:
    registry = NativeAgentRegistry([FakeProvider()])

    assert registry.get("codex").provider_engine == "app-server"
    assert registry.get(" codex ").provider_engine == "app-server"
    assert registry.maybe_get(" codex ") is registry.get("codex")


def test_registry_rejects_duplicate_provider_names() -> None:
    with pytest.raises(ValueError, match="duplicate native provider: codex"):
        NativeAgentRegistry([FakeProvider(), FakeProvider()])


def test_registry_rejects_empty_provider_name() -> None:
    class BadProvider(FakeProvider):
        provider = "  "

    with pytest.raises(ValueError, match="native provider name cannot be empty"):
        NativeAgentRegistry([BadProvider()])


@pytest.mark.parametrize("provider_name", ["claude-cli", "claude-deepseek"])
def test_registry_rejects_claude_engine_as_provider_name(provider_name: str) -> None:
    class BadProvider(FakeProvider):
        provider = provider_name

    with pytest.raises(ValueError, match="Claude engines must not be providers"):
        NativeAgentRegistry([BadProvider()])


def test_registry_lists_provider_summaries() -> None:
    registry = NativeAgentRegistry([FakeProvider()])

    assert registry.list_provider_summaries() == [
        {"provider": "codex", "provider_engine": "app-server"}
    ]
