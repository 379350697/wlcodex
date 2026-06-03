from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ClaudeLocalSession:
    session_id: str
    title: str
    cwd: str
    created_at: str
    updated_at: str
    source_path: str
    entrypoint: str = ""
    version: str = ""
    git_branch: str = ""
    permission_mode: str = ""


@dataclass(frozen=True)
class ClaudeTranscriptEntry:
    role: str
    text: str
    uuid: str
    timestamp: str


class ClaudeLocalSessionIndex:
    def __init__(self, claude_home: Path | None = None) -> None:
        self._claude_home = claude_home or Path.home() / ".claude"

    def list_recent(self, limit: int = 50) -> list[ClaudeLocalSession]:
        sessions = [
            session
            for path in self._session_files()
            if (session := self._session_from_file(path)) is not None
        ]
        sessions.sort(key=lambda session: session.updated_at, reverse=True)
        return sessions[: max(limit, 0)]

    def get(self, session_id: str) -> ClaudeLocalSession | None:
        session_id = session_id.strip()
        if not session_id:
            return None
        direct = self._projects_dir() / f"{session_id}.jsonl"
        if direct.exists():
            return self._session_from_file(direct)
        for path in self._session_files():
            if path.stem == session_id:
                return self._session_from_file(path)
        for path in self._session_files():
            session = self._session_from_file(path)
            if session is not None and session.session_id == session_id:
                return session
        return None

    def read_transcript(self, session_id: str) -> list[ClaudeTranscriptEntry]:
        session = self.get(session_id)
        if session is None:
            return []
        entries: list[ClaudeTranscriptEntry] = []
        for row in _iter_jsonl(Path(session.source_path)):
            role = str(row.get("type") or "")
            if role not in {"user", "assistant"}:
                continue
            text = _message_text(row.get("message"))
            if not text:
                continue
            entries.append(
                ClaudeTranscriptEntry(
                    role=role,
                    text=text,
                    uuid=str(row.get("uuid") or f"{role}-{len(entries)}"),
                    timestamp=str(row.get("timestamp") or ""),
                )
            )
        return entries

    def _projects_dir(self) -> Path:
        return self._claude_home / "projects"

    def _session_files(self) -> list[Path]:
        projects_dir = self._projects_dir()
        if not projects_dir.exists():
            return []
        return [
            path
            for path in projects_dir.rglob("*.jsonl")
            if path.is_file() and "/subagents/" not in path.as_posix()
        ]

    def _session_from_file(self, path: Path) -> ClaudeLocalSession | None:
        session_id = path.stem
        cwd = ""
        created_at = ""
        updated_at = ""
        entrypoint = ""
        version = ""
        git_branch = ""
        permission_mode = ""
        title = ""
        for row in _iter_jsonl(path):
            row_session_id = str(row.get("sessionId") or "")
            if row_session_id:
                session_id = row_session_id
            timestamp = str(row.get("timestamp") or "")
            if timestamp:
                created_at = created_at or timestamp
                updated_at = timestamp
            cwd = str(row.get("cwd") or cwd)
            entrypoint = str(row.get("entrypoint") or entrypoint)
            version = str(row.get("version") or version)
            git_branch = str(row.get("gitBranch") or git_branch)
            permission_mode = str(row.get("permissionMode") or permission_mode)
            title = _session_title_from_row(row) or title
        if not session_id:
            return None
        fallback_time = _mtime_iso(path)
        created_at = created_at or fallback_time
        updated_at = updated_at or fallback_time
        return ClaudeLocalSession(
            session_id=session_id,
            title=title or _fallback_title(session_id),
            cwd=cwd,
            created_at=created_at,
            updated_at=updated_at,
            source_path=str(path),
            entrypoint=entrypoint,
            version=version,
            git_branch=git_branch,
            permission_mode=permission_mode,
        )


def _iter_jsonl(path: Path):
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    yield value
    except OSError:
        return


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    return _content_text(message.get("content")).strip()


def _session_title_from_row(row: dict[str, Any]) -> str:
    for key in ("title", "summary"):
        title = _normalize_title(row.get(key))
        if title:
            return title
    if row.get("type") == "system" and row.get("subtype") == "away_summary":
        return _normalize_title(row.get("content"))
    return ""


def _normalize_title(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    title = " ".join(value.split()).strip()
    title = title.removesuffix("(disable recaps in /config)").strip()
    title = _first_sentence(title)
    return title[:160].strip()


def _first_sentence(title: str) -> str:
    sentence_ends: list[int] = []
    for marker in (". ", "? ", "! "):
        index = title.find(marker)
        if index >= 0:
            sentence_ends.append(index + 1)
    for marker in ("。", "？", "！"):
        index = title.find(marker)
        if index >= 0:
            sentence_ends.append(index + 1)
    if sentence_ends:
        return title[: min(sentence_ends)].strip()
    return title


def _fallback_title(session_id: str) -> str:
    return f"Claude {session_id[:8]}"


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return ""


def _mtime_iso(path: Path) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
