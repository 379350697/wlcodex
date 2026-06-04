from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


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
        normalized = str(Path(cwd).expanduser()) if cwd else ""
        if not normalized:
            return None
        for session in self.list_recent(limit=1000):
            if session.cwd and str(Path(session.cwd).expanduser()) == normalized:
                return session
        return None


def _conversation_files(root: Path) -> list[Path]:
    conversations = root / "conversations"
    if not conversations.exists():
        return []
    files: list[Path] = []
    for suffix in ("*.pb", "*.db"):
        files.extend(path for path in conversations.glob(suffix) if path.is_file())
    return files


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


def _mtime_iso(path: Path) -> str:
    return _timestamp_iso(path.stat().st_mtime)


def _timestamp_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
