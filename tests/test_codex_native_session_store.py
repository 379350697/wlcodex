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


def test_jsonl_index_does_not_override_app_server_session_fields(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.get_or_create_session(
        native_thread_id="thread_source",
        title="Short stable title",
        cwd="/workspace/app",
        source_kind="vscode",
        status="notLoaded",
        activity_at="2026-06-30T10:00:00+00:00",
    )

    updated = store.get_or_create_session(
        native_thread_id="thread_source",
        title="Long raw user prompt from jsonl",
        cwd="/workspace/jsonl",
        source_kind="codex_jsonl",
        status="idle",
        last_turn_id="turn-jsonl",
        activity_at="2026-06-30T10:01:00+00:00",
        metadata={"rollout_path": "/tmp/session.jsonl"},
    )

    assert updated.id == first.id
    assert updated.title == "Short stable title"
    assert updated.cwd == "/workspace/app"
    assert updated.source_kind == "vscode"
    assert updated.status == "notLoaded"
    assert updated.activity_at == "2026-06-30T10:00:00+00:00"
    assert updated.last_turn_id == "turn-jsonl"
    assert updated.metadata["rollout_path"] == "/tmp/session.jsonl"


def test_app_server_source_can_replace_jsonl_index_fields(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.get_or_create_session(
        native_thread_id="thread_source",
        title="Long raw user prompt from jsonl",
        cwd="/workspace/jsonl",
        source_kind="codex_jsonl",
        status="idle",
        activity_at="2026-06-30T10:00:00+00:00",
    )

    updated = store.get_or_create_session(
        native_thread_id="thread_source",
        title="Short stable title",
        cwd="/workspace/app",
        source_kind="vscode",
        status="notLoaded",
        activity_at="2026-06-30T10:01:00+00:00",
    )

    assert updated.title == "Short stable title"
    assert updated.cwd == "/workspace/app"
    assert updated.source_kind == "vscode"
    assert updated.status == "notLoaded"
    assert updated.activity_at == "2026-06-30T10:01:00+00:00"


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


def test_session_metadata_is_persisted_and_merged(tmp_path: Path) -> None:
    store = _store(tmp_path)

    created = store.get_or_create_session(
        native_thread_id="thread_meta",
        metadata={"model": "gpt-5.5", "effort": "xhigh"},
    )
    updated = store.update_session(
        created.id,
        metadata={"service_tier": "priority"},
    )

    assert created.metadata == {"model": "gpt-5.5", "effort": "xhigh"}
    assert updated.metadata == {
        "model": "gpt-5.5",
        "effort": "xhigh",
        "service_tier": "priority",
    }
    assert store.get_by_thread_id("thread_meta").metadata == updated.metadata


def test_session_metadata_drops_exception_name_model_values(tmp_path: Path) -> None:
    store = _store(tmp_path)

    created = store.get_or_create_session(
        native_thread_id="thread_bad_model",
        metadata={"model": "FileNotFoundError", "effort": "xhigh"},
    )

    assert created.metadata == {"effort": "xhigh"}
    assert store.get_by_thread_id("thread_bad_model").metadata == {"effort": "xhigh"}


def test_list_recent_returns_newest_first(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.get_or_create_session(native_thread_id="thread_1")
    second = store.get_or_create_session(native_thread_id="thread_2")
    third = store.get_or_create_session(native_thread_id="thread_3")

    store.update_session(first.id, status="running")
    recent = store.list_recent(limit=10)

    assert [session.id for session in recent] == [third.id, second.id, first.id]
    assert recent[-1].status == "running"
