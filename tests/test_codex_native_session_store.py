from pathlib import Path

from wlcodex.codex_native.session_store import NativeCodexSessionStore
from wlcodex.db import Ledger


def _store(tmp_path: Path) -> NativeCodexSessionStore:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    return NativeCodexSessionStore(ledger)


def test_get_or_create_session_reuses_native_thread_agent_run(tmp_path: Path) -> None:
    store = _store(tmp_path)

    first = store.get_or_create_session(
        native_thread_id="thread_123",
        title="Native task",
        cwd="/workspace",
        source_kind="telegram",
        status="running",
    )
    second = store.get_or_create_session(
        native_thread_id="thread_123",
        title="Different title",
        cwd="/other",
        source_kind="api",
        status="done",
    )

    assert second.id == first.id
    assert second.agent_run_id == first.agent_run_id
    assert second.native_thread_id == "thread_123"

    agent_run = store._ledger.get_agent_run(first.agent_run_id)
    assert agent_run.agent == "codex"
    assert agent_run.role == "codex_native"
    assert agent_run.external_session_id == "thread_123"


def test_update_session_updates_status_and_last_turn_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = store.get_or_create_session(native_thread_id="thread_abc")

    updated = store.update_session(
        session.id,
        status="running",
        last_turn_id="turn_789",
    )

    assert updated.status == "running"
    assert updated.last_turn_id == "turn_789"
    assert store._ledger.get_agent_run(session.agent_run_id).status == "running"


def test_list_recent_returns_newest_first(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.get_or_create_session(native_thread_id="thread_1")
    second = store.get_or_create_session(native_thread_id="thread_2")
    third = store.get_or_create_session(native_thread_id="thread_3")

    store.update_session(first.id, status="running")
    recent = store.list_recent(limit=10)

    assert [session.id for session in recent] == [third.id, second.id, first.id]
    assert recent[-1].status == "running"
