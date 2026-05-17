"""Live Telegram smoke gate.

Gated by WLCODEX_RUN_TELEGRAM_LIVE=1.
Verifies evidence produced by a real human-to-bot Telegram interaction.
Does NOT attempt to fake a Telegram user through Bot API.

NEVER prints bot_token or any other secret to logs, stdout, or reports.
"""

import os
from pathlib import Path
import re
import sqlite3

import pytest

from wlcodex.config import load_config


pytestmark = pytest.mark.skipif(
    os.environ.get("WLCODEX_RUN_TELEGRAM_LIVE") != "1",
    reason="set WLCODEX_RUN_TELEGRAM_LIVE=1 to run live Telegram smoke",
)

# Real Codex thread IDs are UUIDs.  Fake-backend thread IDs are "fake-xxxx".
# This pattern rejects fake prefixes so a misconfigured test can never pass
# against non-real data.
_CODEX_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _config_path() -> Path:
    return Path(os.environ.get("WLCODEX_CONFIG_PATH", "config/wlcodex.toml"))


def _sqlite_path() -> str:
    return os.environ.get(
        "WLCODEX_LIVE_SQLITE_PATH",
        str(load_config(_config_path()).storage.sqlite_path),
    )


def _live_task_id() -> int | None:
    raw = os.environ.get("WLCODEX_LIVE_SMOKE_TASK_ID")
    return int(raw) if raw else None


def test_live_telegram_preflight_env_is_configured() -> None:
    """Pre-launch gate: no chat_id/task_id required before the first message."""
    required = [
        "WLCODEX_TELEGRAM_BOT_TOKEN",
        "WLCODEX_TELEGRAM_ALLOWED_USER_ID",
    ]
    missing = [name for name in required if not os.environ.get(name)]
    assert not missing, f"missing live Telegram env vars: {missing}"

    config = load_config(_config_path())
    allowed_user_id = int(os.environ["WLCODEX_TELEGRAM_ALLOWED_USER_ID"])
    assert allowed_user_id in config.telegram.allowed_user_ids
    assert config.telegram.private_chat_only

    workspace_alias = os.environ.get("WLCODEX_LIVE_WORKSPACE_ALIAS")
    if workspace_alias:
        config.workspace_by_alias(workspace_alias)


def test_live_telegram_task_has_real_ledger_evidence() -> None:
    task_id = _live_task_id()
    if task_id is None:
        pytest.skip(
            "post-interaction smoke requires WLCODEX_LIVE_SMOKE_TASK_ID "
            "after sending a real /task"
        )

    db_path = _sqlite_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert task is not None, f"task #{task_id} not found"
    assert task["telegram_chat_id"] is not None
    expected_chat_id = os.environ.get("WLCODEX_TELEGRAM_CHAT_ID")
    if expected_chat_id:
        assert task["telegram_chat_id"] == int(expected_chat_id)
    assert task["telegram_status_message_id"] is not None
    assert task["codex_thread_id"], "task has no real Codex thread id"
    # Reject fake-backend prefixes and non-UUID thread ids
    assert _CODEX_UUID_RE.match(task["codex_thread_id"]), (
        f"codex_thread_id {task['codex_thread_id']!r} does not look like a "
        f"real Codex UUID — fake-backend evidence is NOT valid smoke evidence"
    )
    assert task["status"] in {"done", "running", "waiting_approval", "paused"}, (
        f"task #{task_id} status is {task['status']} — "
        f"failed/aborted/queued/archived are NOT valid smoke evidence"
    )

    events = conn.execute(
        "SELECT event_type FROM task_events WHERE task_id = ? ORDER BY id ASC",
        (task_id,),
    ).fetchall()
    event_types = {row["event_type"] for row in events}
    assert (
        "task_reserved" in event_types
        or "task_created" in event_types
        or "task_waiting_slot_created" in event_types
    )
    assert "turn_started" in event_types
    if task["status"] == "done":
        assert "turn_completed" in event_types, (
            f"task #{task_id} is done but has no turn_completed event"
        )


def test_live_telegram_approval_evidence_when_required() -> None:
    if os.environ.get("WLCODEX_LIVE_APPROVAL_REQUIRED") != "1":
        pytest.skip("set WLCODEX_LIVE_APPROVAL_REQUIRED=1 after running approval smoke")

    task_id = _live_task_id()
    assert task_id is not None, (
        "WLCODEX_LIVE_APPROVAL_REQUIRED=1 requires WLCODEX_LIVE_SMOKE_TASK_ID"
    )

    db_path = _sqlite_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT status, resolution
        FROM approval_requests
        WHERE task_id = ?
        ORDER BY id ASC
        """,
        (task_id,),
    ).fetchall()
    assert rows, f"task #{task_id} has no approval rows"
    assert any(row["status"] in {"approved", "denied", "cancelled"} for row in rows)
