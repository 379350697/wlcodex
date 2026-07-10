"""Pure path and query helpers for the Native and Relay HTTP surface.

The live-stream server deliberately keeps request mutation and socket handling
in :mod:`server`.  These functions have no server, database, or event-loop
dependency, so route parsing stays deterministic and can be reused by route
tests without instantiating the application.
"""

from __future__ import annotations

from urllib.parse import unquote


def agent_id_from_path(path: str, *, prefix: str, suffix: str) -> int | None:
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    raw = path[len(prefix) : -len(suffix)]
    if not raw.isdigit():
        return None
    return int(raw)


def native_timeline_route_from_path(path: str) -> tuple[str, str, bool] | None:
    prefix = "/api/native/"
    if not path.startswith(prefix):
        return None
    parts = [unquote(part) for part in path[len(prefix) :].split("/") if part]
    if len(parts) == 4 and parts[1] == "sessions" and parts[3] == "timeline":
        return parts[0], parts[2], False
    if (
        len(parts) == 5
        and parts[1] == "sessions"
        and parts[3] == "timeline"
        and parts[4] == "stream"
    ):
        return parts[0], parts[2], True
    return None


def native_messages_route_from_path(path: str) -> tuple[str, str, bool] | None:
    prefix = "/api/native/"
    if not path.startswith(prefix):
        return None
    parts = [unquote(part) for part in path[len(prefix) :].split("/") if part]
    if len(parts) == 4 and parts[1] == "sessions" and parts[3] == "messages":
        return parts[0], parts[2], False
    if (
        len(parts) == 5
        and parts[1] == "sessions"
        and parts[3] == "messages"
        and parts[4] == "stream"
    ):
        return parts[0], parts[2], True
    return None


def relay_task_id_from_ui_path(path: str) -> int | None:
    prefix = "/native/workflows/relay/tasks/"
    if not path.startswith(prefix):
        return None
    raw = path.removeprefix(prefix).strip("/")
    return int(raw) if raw.isdigit() else None


def normalize_relay_api_path(path: str) -> str:
    """Map the historical ``runs`` endpoint to the task contract."""

    if path == "/api/relay/runs":
        return "/api/relay/tasks"
    prefix = "/api/relay/runs/"
    if path.startswith(prefix):
        return "/api/relay/tasks/" + path.removeprefix(prefix)
    return path


def relay_task_api_parts(path: str) -> tuple[int | None, str]:
    prefix = "/api/relay/tasks/"
    if not path.startswith(prefix):
        return None, ""
    raw = path.removeprefix(prefix)
    task_raw, _, suffix_raw = raw.partition("/")
    if not task_raw.isdigit():
        return None, ""
    suffix = f"/{suffix_raw}" if suffix_raw else ""
    return int(task_raw), suffix


def native_provider_route_parts(path: str) -> tuple[str, str]:
    prefix = "/api/native/"
    if not path.startswith(prefix):
        return "", ""
    provider, _, suffix = path[len(prefix) :].partition("/")
    return unquote(provider), suffix


def native_login_provider_from_path(path: str) -> str:
    parts = [part for part in path.split("/") if part]
    if len(parts) == 3 and parts[0] == "native" and parts[2] == "login":
        return unquote(parts[1])
    return ""


def native_page_provider_from_path(path: str) -> str:
    parts = [part for part in path.split("/") if part]
    if len(parts) == 2 and parts[0] == "native":
        return unquote(parts[1])
    return ""


def safe_int(raw: object, *, default: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(0, value)


def optional_nonempty_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
