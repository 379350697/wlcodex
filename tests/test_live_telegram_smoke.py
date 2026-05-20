"""Live Telegram smoke gate.

Gated by WLCODEX_RUN_TELEGRAM_LIVE=1.
Verifies evidence produced by a real human-to-bot Telegram interaction.
Does NOT attempt to fake a Telegram user through Bot API.

NEVER prints bot_token or any other secret to logs, stdout, or reports.
"""

import os
from pathlib import Path
import sqlite3

import pytest

from wlcodex.config import load_config


pytestmark = pytest.mark.skipif(
    os.environ.get("WLCODEX_RUN_TELEGRAM_LIVE") != "1",
    reason="set WLCODEX_RUN_TELEGRAM_LIVE=1 to run live Telegram smoke",
)

def _config_path() -> Path:
    return Path(os.environ.get("WLCODEX_CONFIG_PATH", "config/wlcodex.toml"))


def _sqlite_path() -> str:
    return os.environ.get(
        "WLCODEX_LIVE_SQLITE_PATH",
        str(load_config(_config_path()).storage.sqlite_path),
    )


def _live_conversation_id(conn: sqlite3.Connection) -> int | None:
    raw = os.environ.get("WLCODEX_LIVE_CONVERSATION_ID")
    if raw:
        return int(raw)
    chat_id = os.environ.get("WLCODEX_TELEGRAM_CHAT_ID")
    if chat_id:
        row = conn.execute(
            """
            SELECT id FROM conversation_sessions
            WHERE chat_id = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (int(chat_id),),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT id FROM conversation_sessions
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
    return int(row["id"]) if row is not None else None


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


def test_live_telegram_workbench_has_runtime_event_evidence() -> None:
    db_path = _sqlite_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    conversation_id = _live_conversation_id(conn)
    if conversation_id is None:
        pytest.skip("post-interaction smoke requires a real /new + plain text Workbench")

    conversation = conn.execute(
        "SELECT * FROM conversation_sessions WHERE id = ?",
        (conversation_id,),
    ).fetchone()
    assert conversation is not None, f"conversation #{conversation_id} not found"
    expected_chat_id = os.environ.get("WLCODEX_TELEGRAM_CHAT_ID")
    if expected_chat_id:
        assert conversation["chat_id"] == int(expected_chat_id)

    runtime_rows = conn.execute(
        """
        SELECT event_type, visibility FROM runtime_events
        WHERE conversation_id = ?
        ORDER BY id ASC
        """,
        (conversation_id,),
    ).fetchall()
    event_types = {row["event_type"] for row in runtime_rows}
    assert "user.message.received" in event_types
    assert any(
        event in event_types
        for event in {"run.started", "agent.run.started", "run.completed"}
    )
    assert all(row["visibility"] in {"user", "operator", "internal"} for row in runtime_rows)

    agent_runs = conn.execute(
        """
        SELECT agent, external_session_id FROM agent_runs
        WHERE conversation_id = ?
        ORDER BY id ASC
        """,
        (conversation_id,),
    ).fetchall()
    assert agent_runs, f"conversation #{conversation_id} has no agent runs"
    assert any(row["external_session_id"] for row in agent_runs), (
        f"conversation #{conversation_id} has no persisted native agent session ref"
    )


def test_live_telegram_output_is_not_fragment_spam() -> None:
    import json

    db_path = _sqlite_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT event_type, payload_json
        FROM runtime_events
        WHERE event_type IN ('telegram.delivery.enqueued', 'telegram.message.sent', 'telegram.message.edited')
        ORDER BY id DESC
        LIMIT 80
        """
    ).fetchall()

    previews = []
    tiny_fragments = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        text = payload.get("text_preview", "")
        if "正在" in text or "运行" in text:
            previews.append(text)
        if text in {"我", "查", "到", "的"}:
            tiny_fragments.append(text)

    assert previews
    assert tiny_fragments == []


def test_live_telegram_approval_evidence_when_required() -> None:
    if os.environ.get("WLCODEX_LIVE_APPROVAL_REQUIRED") != "1":
        pytest.skip("set WLCODEX_LIVE_APPROVAL_REQUIRED=1 after running approval smoke")

    db_path = _sqlite_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conversation_id = _live_conversation_id(conn)
    assert conversation_id is not None, (
        "WLCODEX_LIVE_APPROVAL_REQUIRED=1 requires WLCODEX_LIVE_CONVERSATION_ID "
        "or an existing live Workbench conversation"
    )
    rows = conn.execute(
        """
        SELECT ar.status, ar.resolution
        FROM approval_requests AS ar
        JOIN agent_runs AS ag ON ag.hidden_task_id = ar.task_id
        WHERE ag.conversation_id = ?
        ORDER BY ar.id ASC
        """,
        (conversation_id,),
    ).fetchall()
    assert rows, f"conversation #{conversation_id} has no approval rows"
    assert any(row["status"] in {"approved", "denied", "cancelled"} for row in rows)
