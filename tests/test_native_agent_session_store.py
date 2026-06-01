from pathlib import Path

from wlcodex.db import Ledger
from wlcodex.native_agents.session_store import NativeAgentSessionStore


def _store(tmp_path: Path) -> NativeAgentSessionStore:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    return NativeAgentSessionStore(ledger)


def test_get_or_create_session_is_unique_by_provider_engine_and_session(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    first = store.get_or_create_session(
        provider="claude",
        provider_engine="cli-local",
        native_session_id="session-1",
        title="Local",
        cwd="/repo",
        source_kind="claude_cli_local",
        status="running",
    )
    second = store.get_or_create_session(
        provider="claude",
        provider_engine="sdk-deepseek",
        native_session_id="session-1",
        title="SDK",
        cwd="/repo",
        source_kind="claude_sdk_deepseek",
        status="running",
    )

    assert first.id != second.id
    assert first.agent_run_id != second.agent_run_id
    assert first.provider_engine == "cli-local"
    assert second.provider_engine == "sdk-deepseek"


def test_get_or_create_session_reuses_existing_row(tmp_path: Path) -> None:
    store = _store(tmp_path)

    first = store.get_or_create_session(
        provider="codex",
        provider_engine="app-server",
        native_session_id="thread-1",
        title="old",
        cwd="/repo",
        source_kind="codex_native",
        status="queued",
    )
    second = store.get_or_create_session(
        provider="codex",
        provider_engine="app-server",
        native_session_id="thread-1",
        title="new",
        cwd="/repo",
        source_kind="codex_native",
        status="running",
        last_turn_id="turn-1",
    )

    assert second.id == first.id
    assert second.agent_run_id == first.agent_run_id
    assert second.title == "new"
    assert second.status == "running"
    assert second.last_turn_id == "turn-1"


def test_get_or_create_session_keeps_existing_status_when_omitted(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first = store.get_or_create_session(
        provider="codex",
        provider_engine="app-server",
        native_session_id="thread-2",
        title="existing",
        cwd="/repo",
        source_kind="codex_native",
        status="running",
        last_turn_id="turn-2",
    )

    second = store.get_or_create_session(
        provider="codex",
        provider_engine="app-server",
        native_session_id="thread-2",
    )

    assert second.id == first.id
    assert second.status == "running"
    assert second.last_turn_id == "turn-2"


def test_list_recent_filters_by_provider(tmp_path: Path) -> None:
    store = _store(tmp_path)
    codex = store.get_or_create_session(
        provider="codex",
        provider_engine="app-server",
        native_session_id="thread-1",
        title="Codex",
        cwd="/repo",
        source_kind="codex_native",
        status="done",
    )
    claude = store.get_or_create_session(
        provider="claude",
        provider_engine="sdk-deepseek",
        native_session_id="session-1",
        title="Claude",
        cwd="/repo",
        source_kind="claude_sdk_deepseek",
        status="running",
    )

    assert [session.id for session in store.list_recent(provider="claude")] == [
        claude.id
    ]
    assert [session.id for session in store.list_recent(provider="codex")] == [
        codex.id
    ]


def test_list_recent_can_filter_by_provider_engine(tmp_path: Path) -> None:
    store = _store(tmp_path)
    cli = store.get_or_create_session(
        provider="claude",
        provider_engine="cli-local",
        native_session_id="cli-1",
    )
    for index in range(5):
        store.get_or_create_session(
            provider="claude",
            provider_engine="sdk-deepseek",
            native_session_id=f"sdk-{index}",
        )

    assert store.list_recent(provider="claude", provider_engine="cli-local") == [cli]
