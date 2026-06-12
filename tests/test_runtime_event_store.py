"""Tests for RuntimeEventStore — append, query, redaction, immutability."""

from __future__ import annotations

from pathlib import Path

import pytest

from wlcodex.db import Ledger
from wlcodex.runtime_events import (
    MAX_PAYLOAD_STRING_LENGTH,
    REDACTED_PLACEHOLDER,
    AggregateType,
    EventSource,
    EventType,
    RuntimeEvent,
    Visibility,
    now_iso,
    redact_payload,
    safe_text_preview,
)
from wlcodex.runtime_event_store import RuntimeEventStore

pytestmark = pytest.mark.slow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(**overrides: object) -> RuntimeEvent:
    defaults: dict[str, object] = {
        "schema_version": 1,
        "event_type": EventType.AGENT_RUN_STARTED,
        "aggregate_type": AggregateType.AGENT_RUN,
        "aggregate_id": "ar-1",
        "correlation_id": "corr-1",
        "source": EventSource.CLAUDE,
        "actor": "claude",
        "visibility": Visibility.INTERNAL,
        "payload": {"model": "claude-sonnet-4-6", "input_tokens": 150},
        "occurred_at": now_iso(),
        "conversation_id": 1,
        "agent_run_id": 1,
    }
    merged = {**defaults, **overrides}
    return RuntimeEvent(**merged)  # type: ignore[arg-type]


def _store(tmp_path: Path) -> RuntimeEventStore:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    return RuntimeEventStore(ledger._conn)


# ---------------------------------------------------------------------------
# Append + read back
# ---------------------------------------------------------------------------

def test_append_and_read_by_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    event = _make_event()

    saved = store.append(event)

    assert saved.id > 0
    assert saved.event_type == EventType.AGENT_RUN_STARTED
    assert saved.correlation_id == "corr-1"
    assert saved.payload == {"model": "claude-sonnet-4-6", "input_tokens": 150}

    loaded = store.get_by_id(saved.id)
    assert loaded == saved


def test_append_does_not_require_task_event(tmp_path: Path) -> None:
    """AC: appending a runtime event does not require any existing task event."""
    store = _store(tmp_path)
    event = _make_event(task_id=None, conversation_id=None, agent_run_id=None)
    saved = store.append(event)
    assert saved.id > 0


def test_append_notifies_registered_projector_after_commit(tmp_path: Path) -> None:
    """RuntimeEventStore.append is the live projection boundary."""
    store = _store(tmp_path)
    seen: list[RuntimeEvent] = []

    store.add_projector(seen.append)
    saved = store.append(_make_event(payload={"n": 1}))

    assert seen == [saved]
    assert seen[0].id > 0


# ---------------------------------------------------------------------------
# Query by correlation_id
# ---------------------------------------------------------------------------

def test_list_by_correlation_returns_events_in_id_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    a = store.append(_make_event(correlation_id="corr-a", payload={"n": 1}))
    b = store.append(_make_event(correlation_id="corr-a", payload={"n": 2}))
    c = store.append(_make_event(correlation_id="corr-a", payload={"n": 3}))

    events = store.list_by_correlation("corr-a")
    ids = [e.id for e in events]
    assert ids == [a.id, b.id, c.id]
    assert [e.payload["n"] for e in events] == [1, 2, 3]


def test_list_by_correlation_respects_limit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for i in range(10):
        store.append(_make_event(correlation_id="corr-x", payload={"n": i}))

    events = store.list_by_correlation("corr-x", limit=3)
    assert len(events) == 3


def test_list_by_correlation_empty(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.list_by_correlation("nonexistent") == []


# ---------------------------------------------------------------------------
# Query by agent_run_id
# ---------------------------------------------------------------------------

def test_list_by_agent_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(_make_event(agent_run_id=42, payload={"step": 1}))
    store.append(_make_event(agent_run_id=42, payload={"step": 2}))
    store.append(_make_event(agent_run_id=99, payload={"step": "other"}))

    events = store.list_by_agent_run(42)
    assert len(events) == 2
    assert all(e.agent_run_id == 42 for e in events)


def test_list_by_agent_run_after_returns_events_after_cursor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.append(_make_event(agent_run_id=42, payload={"n": 1}))
    second = store.append(_make_event(agent_run_id=42, payload={"n": 2}))
    store.append(_make_event(agent_run_id=99, payload={"n": "other"}))
    third = store.append(_make_event(agent_run_id=42, payload={"n": 3}))

    events = store.list_by_agent_run_after(42, after_id=first.id, limit=20)

    assert [event.id for event in events] == [second.id, third.id]
    assert [event.payload["n"] for event in events] == [2, 3]


def test_list_by_agent_run_after_respects_limit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for index in range(5):
        store.append(_make_event(agent_run_id=42, payload={"n": index}))

    events = store.list_by_agent_run_after(42, after_id=0, limit=2)

    assert len(events) == 2
    assert [event.payload["n"] for event in events] == [0, 1]


def test_list_by_agent_run_after_rejects_non_positive_limit(tmp_path: Path) -> None:
    store = _store(tmp_path)

    events = store.list_by_agent_run_after(42, after_id=0, limit=0)

    assert events == []


def test_payload_item_ids_by_agent_run_reads_item_id_variants(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.append(
        _make_event(
            agent_run_id=7,
            payload={"itemId": "item-official", "text": "hello"},
        )
    )
    store.append(
        _make_event(
            agent_run_id=7,
            payload={"item_id": "item-legacy", "text": "world"},
        )
    )
    store.append(
        _make_event(
            agent_run_id=8,
            payload={"itemId": "item-other-run"},
        )
    )

    assert store.payload_item_ids_by_agent_run(7) == {
        "item-official",
        "item-legacy",
    }
    assert store.payload_item_turn_ids_by_agent_run(7) == {
        "item-official": set(),
        "item-legacy": set(),
    }


def test_payload_item_turn_ids_by_agent_run_reads_turn_id_variants(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.append(
        _make_event(
            agent_run_id=7,
            payload={"itemId": "item-one", "turnId": "turn-official"},
        )
    )
    store.append(
        _make_event(
            agent_run_id=7,
            payload={"itemId": "item-one", "native_turn_id": "turn-fallback"},
        )
    )
    store.append(
        _make_event(
            agent_run_id=7,
            payload={"item_id": "item-two", "native_turn_id": "turn-two"},
        )
    )

    assert store.payload_item_turn_ids_by_agent_run(7) == {
        "item-one": {"turn-official", "turn-fallback"},
        "item-two": {"turn-two"},
    }


# ---------------------------------------------------------------------------
# Query by conversation_id
# ---------------------------------------------------------------------------

def test_list_by_conversation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(_make_event(conversation_id=7, payload={"msg": "a"}))
    store.append(_make_event(conversation_id=7, payload={"msg": "b"}))
    store.append(_make_event(conversation_id=8, payload={"msg": "c"}))

    events = store.list_by_conversation(7)
    assert len(events) == 2
    assert all(e.conversation_id == 7 for e in events)


def test_list_recent_for_conversation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for i in range(5):
        store.append(_make_event(conversation_id=5, payload={"n": i}))

    events = store.list_recent_for_conversation(5, limit=3)
    assert len(events) == 3
    assert [e.payload["n"] for e in events] == [2, 3, 4]


# ---------------------------------------------------------------------------
# Query by orchestration_run_id
# ---------------------------------------------------------------------------

def test_list_by_orchestration_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append(_make_event(orchestration_run_id=100, payload={"phase": "analysis"}))
    store.append(_make_event(orchestration_run_id=100, payload={"phase": "implementation"}))
    store.append(_make_event(orchestration_run_id=200, payload={"phase": "unrelated"}))

    events = store.list_by_orchestration_run(100)
    assert len(events) == 2
    assert all(e.orchestration_run_id == 100 for e in events)


# ---------------------------------------------------------------------------
# Immutability — payloads are copied, not mutated after append
# ---------------------------------------------------------------------------

def test_payload_is_copied_not_mutated_after_append(tmp_path: Path) -> None:
    store = _store(tmp_path)
    original_payload = {"value": "hello"}

    saved = store.append(_make_event(payload=original_payload))

    # Mutating the caller's dict must not affect the stored event.
    original_payload["value"] = "MUTATED"
    original_payload["extra"] = "NEW"

    loaded = store.get_by_id(saved.id)
    assert loaded.payload == {"value": "hello"}
    assert "extra" not in loaded.payload


def test_separate_appends_are_independent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    shared: dict[str, object] = {"count": 0}

    a = store.append(_make_event(payload=shared))
    shared["count"] = 99
    b = store.append(_make_event(payload=shared))

    assert store.get_by_id(a.id).payload == {"count": 0}
    assert store.get_by_id(b.id).payload == {"count": 99}


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

def test_redact_keys_containing_secret(tmp_path: Path) -> None:
    store = _store(tmp_path)
    event = _make_event(payload={
        "api_secret": "sk-abc123",
        "auth_token": "Bearer xyz",
        "Authorization": "Basic dXNlcjpwYXNz",
        "headers": {"X-Api-Key": "key-999", "Content-Type": "application/json"},
        "safe_field": "visible",
    })
    saved = store.append(event)

    assert saved.payload["api_secret"] == REDACTED_PLACEHOLDER
    assert saved.payload["auth_token"] == REDACTED_PLACEHOLDER
    assert saved.payload["Authorization"] == REDACTED_PLACEHOLDER
    # "headers" itself is not redacted — only auth-like sub-keys are.
    assert saved.payload["headers"]["X-Api-Key"] == REDACTED_PLACEHOLDER
    assert saved.payload["headers"]["Content-Type"] == "application/json"
    assert saved.payload["safe_field"] == "visible"


def test_redact_nested_secrets(tmp_path: Path) -> None:
    store = _store(tmp_path)
    event = _make_event(payload={
        "config": {
            "db_password": "super-secret",
            "host": "localhost",
            "nested": {"token": "nested-token"},
        }
    })
    saved = store.append(event)

    cfg = saved.payload["config"]
    assert cfg["db_password"] == REDACTED_PLACEHOLDER
    assert cfg["host"] == "localhost"
    assert cfg["nested"]["token"] == REDACTED_PLACEHOLDER


def test_redact_in_list_of_dicts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    event = _make_event(payload={
        "items": [
            {"name": "a", "secret": "s1"},
            {"name": "b", "token": "t1"},
        ]
    })
    saved = store.append(event)

    assert saved.payload["items"][0] == {"name": "a", "secret": REDACTED_PLACEHOLDER}
    assert saved.payload["items"][1] == {"name": "b", "token": REDACTED_PLACEHOLDER}


def test_redact_credential_and_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    event = _make_event(payload={
        "credential": "my-cred",
        "api_key": "my-key",
        "signing_key": "sig-123",
    })
    saved = store.append(event)

    assert saved.payload["credential"] == REDACTED_PLACEHOLDER
    assert saved.payload["api_key"] == REDACTED_PLACEHOLDER
    assert saved.payload["signing_key"] == REDACTED_PLACEHOLDER


def test_redaction_happens_before_sqlite(tmp_path: Path) -> None:
    """AC: Redaction is applied before payload JSON reaches SQLite."""
    store = _store(tmp_path)
    event = _make_event(payload={"token": "raw-secret"})
    saved = store.append(event)

    # Verify via direct SQL that the stored JSON is already redacted.
    row = store._conn.execute(
        "SELECT payload_json FROM runtime_events WHERE id = ?", (saved.id,)
    ).fetchone()
    raw_json = str(row["payload_json"])
    assert "raw-secret" not in raw_json
    assert REDACTED_PLACEHOLDER in raw_json


# ---------------------------------------------------------------------------
# Payload length caps
# ---------------------------------------------------------------------------

def test_payload_string_length_capped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    long_text = "x" * (MAX_PAYLOAD_STRING_LENGTH + 500)
    event = _make_event(payload={"output": long_text})
    saved = store.append(event)

    capped = saved.payload["output"]
    assert len(capped) < len(long_text)
    assert capped.endswith("...<truncated>")
    assert len(capped) == MAX_PAYLOAD_STRING_LENGTH + len("...<truncated>")


def test_short_strings_are_not_truncated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    event = _make_event(payload={"output": "short"})
    saved = store.append(event)
    assert saved.payload["output"] == "short"


def test_truncation_and_redaction_work_together(tmp_path: Path) -> None:
    store = _store(tmp_path)
    long_secret = "sk-" + ("x" * (MAX_PAYLOAD_STRING_LENGTH + 1000))
    event = _make_event(payload={"secret": long_secret, "output": "ok"})
    saved = store.append(event)

    # Secret is redacted BEFORE truncation applies, so it's just [REDACTED].
    assert saved.payload["secret"] == REDACTED_PLACEHOLDER
    assert saved.payload["output"] == "ok"


# ---------------------------------------------------------------------------
# Schema migration idempotency
# ---------------------------------------------------------------------------

def test_migration_is_idempotent_with_runtime_events(tmp_path: Path) -> None:
    ledger = Ledger.open(tmp_path / "wlcodex.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)

    # Second migration must not crash.
    ledger.migrate()

    # Must still be able to append and query after re-migration.
    saved = store.append(_make_event())
    assert store.get_by_id(saved.id) == saved


# ---------------------------------------------------------------------------
# get_by_id raises on unknown id
# ---------------------------------------------------------------------------

def test_get_by_id_raises_keyerror_on_unknown(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(KeyError, match="unknown runtime event id"):
        store.get_by_id(99999)


# ---------------------------------------------------------------------------
# All event type constants are unique
# ---------------------------------------------------------------------------

def test_event_type_constants_are_unique() -> None:
    values = [
        v for k, v in vars(EventType).items()
        if not k.startswith("_") and isinstance(v, str)
    ]
    assert len(values) == len(set(values)), "Duplicate event type constants"


# ---------------------------------------------------------------------------
# multiple correlation_ids coexist
# ---------------------------------------------------------------------------

def test_multiple_correlations_are_isolated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    a = store.append(_make_event(correlation_id="c1", payload={"n": 1}))
    b = store.append(_make_event(correlation_id="c2", payload={"n": 2}))

    assert [e.id for e in store.list_by_correlation("c1")] == [a.id]
    assert [e.id for e in store.list_by_correlation("c2")] == [b.id]


# ---------------------------------------------------------------------------
# Pure redact_payload unit tests
# ---------------------------------------------------------------------------

def test_redact_payload_pure_leaves_input_unmodified() -> None:
    original = {"token": "secret", "name": "test"}
    result = redact_payload(original)
    assert result == {"token": REDACTED_PLACEHOLDER, "name": "test"}
    assert original == {"token": "secret", "name": "test"}


def test_redact_payload_handles_empty() -> None:
    assert redact_payload({}) == {}


def test_redact_payload_handles_non_string_values() -> None:
    payload = {"count": 42, "active": True, "ratio": 3.14, "data": None}
    assert redact_payload(payload) == payload


def test_safe_text_preview_redacts_common_secret_patterns() -> None:
    text = "deploy password=abc123 token=secret123 api_key: sk-live-abc"

    preview = safe_text_preview(text)

    assert "abc123" not in preview
    assert "secret123" not in preview
    assert "sk-live-abc" not in preview
    assert "<redacted>" in preview
    assert len(preview) <= 200


def test_redact_payload_content_redacts_textual_payload_fields() -> None:
    payload = {
        "text_preview": "deploy password=abc123 token=secret123",
        "text": "deploy password=abc123 token=secret123",
        "note": "plain status stays visible",
    }

    result = redact_payload(payload)

    assert "abc123" not in result["text_preview"]
    assert "secret123" not in result["text_preview"]
    assert "abc123" not in result["text"]
    assert "secret123" not in result["text"]
    assert result["note"] == "plain status stays visible"
