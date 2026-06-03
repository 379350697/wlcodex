from __future__ import annotations

import json
from pathlib import Path

from wlcodex.native_agents.claude_local_sessions import ClaudeLocalSessionIndex


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_lists_local_claude_sessions_without_using_message_text_for_title(
    tmp_path: Path,
) -> None:
    session_path = (
        tmp_path
        / ".claude"
        / "projects"
        / "-Users-wl-projects-wlcodex"
        / "11111111-1111-4111-8111-111111111111.jsonl"
    )
    _write_jsonl(
        session_path,
        [
            {
                "type": "user",
                "sessionId": "11111111-1111-4111-8111-111111111111",
                "timestamp": "2026-06-03T08:36:15.853Z",
                "cwd": "/Users/wl/projects/wlcodex",
                "entrypoint": "sdk-py",
                "version": "2.1.161",
                "gitBranch": "main",
                "message": {"role": "user", "content": "private prompt text"},
            },
            {
                "type": "assistant",
                "sessionId": "11111111-1111-4111-8111-111111111111",
                "timestamp": "2026-06-03T08:36:19.298Z",
                "cwd": "/Users/wl/projects/wlcodex",
                "entrypoint": "sdk-py",
                "version": "2.1.161",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "private answer text"}],
                },
            },
        ],
    )

    sessions = ClaudeLocalSessionIndex(tmp_path / ".claude").list_recent()

    assert len(sessions) == 1
    session = sessions[0]
    assert session.session_id == "11111111-1111-4111-8111-111111111111"
    assert session.cwd == "/Users/wl/projects/wlcodex"
    assert session.updated_at == "2026-06-03T08:36:19.298Z"
    assert session.entrypoint == "sdk-py"
    assert session.version == "2.1.161"
    assert session.title == "Claude 11111111"
    assert "private" not in session.title


def test_reads_transcript_text_for_selected_session_only(tmp_path: Path) -> None:
    session_path = (
        tmp_path
        / ".claude"
        / "projects"
        / "-Users-wl-projects-wlcodex"
        / "22222222-2222-4222-8222-222222222222.jsonl"
    )
    _write_jsonl(
        session_path,
        [
            {
                "type": "user",
                "uuid": "user-1",
                "sessionId": "22222222-2222-4222-8222-222222222222",
                "timestamp": "2026-06-03T08:36:15.853Z",
                "message": {"role": "user", "content": "hello"},
            },
            {
                "type": "assistant",
                "uuid": "assistant-1",
                "sessionId": "22222222-2222-4222-8222-222222222222",
                "timestamp": "2026-06-03T08:36:19.298Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "hidden reasoning"},
                        {"type": "text", "text": "world"},
                    ],
                },
            },
            {
                "type": "tool",
                "sessionId": "22222222-2222-4222-8222-222222222222",
                "timestamp": "2026-06-03T08:36:20.000Z",
            },
        ],
    )

    entries = ClaudeLocalSessionIndex(tmp_path / ".claude").read_transcript(
        "22222222-2222-4222-8222-222222222222"
    )

    assert [(entry.role, entry.text, entry.uuid) for entry in entries] == [
        ("user", "hello", "user-1"),
        ("assistant", "world", "assistant-1"),
    ]
