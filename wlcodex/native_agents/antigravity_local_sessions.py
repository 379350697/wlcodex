from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AntigravityLocalSession:
    session_id: str
    title: str
    cwd: str
    created_at: str
    updated_at: str
    source_path: str
    brain_path: str = ""
    source_root: str = ""


@dataclass(frozen=True)
class AntigravityTranscriptEntry:
    role: str
    text: str


@dataclass(frozen=True)
class AntigravityTranscriptReadResult:
    entries: tuple[AntigravityTranscriptEntry, ...] = ()
    authority: str = "unavailable"
    error: str = ""


class AntigravityLocalSessionIndex:
    def __init__(self, roots: tuple[Path, ...] | None = None) -> None:
        self._roots = roots or (
            Path.home() / ".gemini" / "antigravity-cli",
            Path.home() / ".gemini" / "antigravity",
        )

    def list_recent(self, limit: int = 50) -> list[AntigravityLocalSession]:
        sessions: list[AntigravityLocalSession] = []
        for root in self._roots:
            cwd_by_session = _cwd_by_session(root)
            for path in _conversation_files(root):
                session = _session_from_file(root, path, cwd_by_session)
                if session is not None:
                    sessions.append(session)
        sessions.sort(key=lambda session: session.updated_at, reverse=True)
        return sessions[: max(limit, 0)]

    def get(self, session_id: str) -> AntigravityLocalSession | None:
        session_id = session_id.strip()
        if not session_id:
            return None
        for root in self._roots:
            for suffix in (".pb", ".db"):
                direct = root / "conversations" / f"{session_id}{suffix}"
                if direct.exists():
                    return _session_from_file(root, direct, _cwd_by_session(root))
        for session in self.list_recent(limit=1000):
            if session.session_id == session_id:
                return session
        return None

    def latest_for_cwd(self, cwd: str) -> AntigravityLocalSession | None:
        if not cwd:
            return None
        for session in self.list_recent(limit=1000):
            if session.cwd and _same_cwd(session.cwd, cwd):
                return session
        return None

    def read_transcript(self, session_id: str) -> AntigravityTranscriptReadResult:
        session = self.get(session_id)
        if session is None:
            return AntigravityTranscriptReadResult(error="session_not_found")
        return _read_transcript_file(Path(session.source_path))


def _conversation_files(root: Path) -> list[Path]:
    conversations = root / "conversations"
    if not conversations.exists():
        return []
    files: list[Path] = []
    for suffix in ("*.pb", "*.db"):
        files.extend(path for path in conversations.glob(suffix) if path.is_file())
    return files


def _read_transcript_file(path: Path) -> AntigravityTranscriptReadResult:
    if path.suffix == ".db":
        return _read_sqlite_transcript(path)
    return AntigravityTranscriptReadResult(error=f"unsupported_{path.suffix.lstrip('.')}")


def _read_sqlite_transcript(path: Path) -> AntigravityTranscriptReadResult:
    entries: list[AntigravityTranscriptEntry] = []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return AntigravityTranscriptReadResult(error=f"sqlite_open_failed:{exc}")
    try:
        conn.row_factory = sqlite3.Row
        tables = [
            str(row["name"])
            for row in conn.execute(
                "select name from sqlite_schema where type='table' order by name"
            )
        ]
        for table in tables:
            columns = [str(row["name"]) for row in conn.execute(f"pragma table_info({_quote_identifier(table)})")]
            if not columns:
                continue
            selected = ", ".join(_quote_identifier(column) for column in columns)
            for row in conn.execute(
                f"select {selected} from {_quote_identifier(table)} limit 1000"
            ):
                values = {column: row[column] for column in columns}
                role = _transcript_role(values)
                text = _transcript_text(values)
                if role and text:
                    entries.append(AntigravityTranscriptEntry(role=role, text=text))
    except sqlite3.Error as exc:
        return AntigravityTranscriptReadResult(error=f"sqlite_read_failed:{exc}")
    finally:
        conn.close()
    if not entries:
        return AntigravityTranscriptReadResult(error="no_transcript_rows")
    return AntigravityTranscriptReadResult(entries=tuple(entries), authority="local")


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _transcript_role(values: dict[str, Any]) -> str:
    for key in ("role", "author", "sender", "speaker"):
        if key not in values:
            continue
        role = _normalize_transcript_role(_text_value(values[key]))
        if role:
            return role
    for value in values.values():
        role = _role_from_jsonish(value)
        if role:
            return role
    return ""


def _role_from_jsonish(value: Any) -> str:
    parsed = _parse_jsonish(value)
    if isinstance(parsed, dict):
        for key in ("role", "author", "sender", "speaker"):
            role = _normalize_transcript_role(_text_value(parsed.get(key)))
            if role:
                return role
    return ""


def _normalize_transcript_role(value: str) -> str:
    lowered = value.strip().lower()
    if lowered in {"assistant", "model", "agent", "ai"}:
        return "assistant"
    if lowered in {"user", "human"}:
        return "user"
    return ""


def _transcript_text(values: dict[str, Any]) -> str:
    for key in ("text", "content", "message", "output", "response", "prompt"):
        if key not in values:
            continue
        text = _text_value(values[key])
        if text:
            return text
    for value in values.values():
        text = _text_from_jsonish(value)
        if text:
            return text
    return ""


def _text_from_jsonish(value: Any) -> str:
    parsed = _parse_jsonish(value)
    return _text_value(parsed)


def _parse_jsonish(value: Any) -> Any:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _text_value(value: Any) -> str:
    value = _parse_jsonish(value)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, list):
        parts = [_text_value(item) for item in value]
        return "".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("text", "content", "message", "output", "response"):
            text = _text_value(value.get(key))
            if text:
                return text
    return ""


def _cwd_by_session(root: Path) -> dict[str, str]:
    path = root / "cache" / "last_conversations.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(session_id): str(cwd)
        for cwd, session_id in value.items()
        if isinstance(cwd, str) and isinstance(session_id, str)
    }


def _session_from_file(
    root: Path,
    path: Path,
    cwd_by_session: dict[str, str],
) -> AntigravityLocalSession | None:
    session_id = path.stem
    if not session_id:
        return None
    brain = root / "brain" / session_id
    updated_at = _session_updated_at(path, brain)
    return AntigravityLocalSession(
        session_id=session_id,
        title=_title_from_brain(brain) or _fallback_title(session_id),
        cwd=cwd_by_session.get(session_id, ""),
        created_at=_mtime_iso(path),
        updated_at=updated_at,
        source_path=str(path),
        brain_path=str(brain) if brain.exists() else "",
        source_root=str(root),
    )


def _session_updated_at(path: Path, brain: Path) -> str:
    latest = path.stat().st_mtime
    if brain.exists():
        for item in brain.rglob("*"):
            if item.is_file():
                latest = max(latest, item.stat().st_mtime)
    return _timestamp_iso(latest)


def _title_from_brain(brain: Path) -> str:
    if not brain.exists():
        return ""
    for name in ("task.md", "walkthrough.md", "implementation_plan.md"):
        title = _title_from_markdown(brain / name)
        if title:
            return title
    return ""


def _title_from_markdown(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    fallback = ""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title:
                return title[:160]
        fallback = fallback or stripped
    return fallback[:160]


def _fallback_title(session_id: str) -> str:
    return f"Antigravity {session_id[:8]}"


def _same_cwd(left: str, right: str) -> bool:
    return _normalized_cwd(left) == _normalized_cwd(right)


def _normalized_cwd(cwd: str) -> str:
    try:
        return str(Path(cwd).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        return str(Path(cwd).expanduser())


def _mtime_iso(path: Path) -> str:
    return _timestamp_iso(path.stat().st_mtime)


def _timestamp_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
