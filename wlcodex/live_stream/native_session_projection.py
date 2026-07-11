"""Read-only Native session view models.

These projections intentionally have no storage or routing dependency.  The
server supplies cache reads and manages background refreshes; GET/SSE callers
receive an honest cache/daemon/failure presentation without causing writes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from wlcodex.codex_native.controller import build_native_session_presentation
from wlcodex.jsonrpc import JsonRpcTimeout


async def build_native_session_payload(
    provider_name: str,
    target: Any,
    native_thread_id: str,
    *,
    read_cached: Callable[[Any, str], Awaitable[dict[str, Any] | None]],
    sync_error: str,
    sync_pending: bool,
    timeout_seconds: float,
    json_object: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    cached = await read_cached(target, native_thread_id)
    if cached is not None:
        payload = dict(cached)
        payload.setdefault("native_provider", provider_name)
        payload["native_sync_pending"] = sync_pending
        if sync_error:
            payload["native_sync_error"] = sync_error
        payload["presentation"] = build_native_session_presentation(
            payload,
            sync_error=sync_error,
        )
        return payload
    try:
        read_only_session = getattr(target, "peek_session", None)
        if read_only_session is None:
            # ``read_session`` can import/project history, so it is never a
            # fallback for an observation-only page request.
            raise RuntimeError("native provider does not expose a read-only session snapshot")
        result = await asyncio.wait_for(
            read_only_session(native_thread_id),
            timeout=timeout_seconds,
        )
        payload = json_object(result)
        payload.setdefault("native_provider", provider_name)
        payload.setdefault("native_session_source", "daemon")
        payload["native_sync_pending"] = False
        payload["presentation"] = build_native_session_presentation(payload)
        return payload
    except KeyError:
        raise
    except (asyncio.TimeoutError, JsonRpcTimeout) as exc:
        failure = str(exc) or "native session sync timed out"
    except Exception as exc:
        failure = str(exc) or "native session sync failed"
    payload = {
        "native_thread_id": native_thread_id,
        "native_provider": provider_name,
        "native_session_source": "stub",
        "native_sync_error": failure,
        "native_sync_pending": False,
        "native_sync_recovery": "请使用同步操作重试；缓存恢复后会显示最后成功更新时间。",
        "thread": {"id": native_thread_id, "threadId": native_thread_id},
    }
    payload["presentation"] = build_native_session_presentation(payload, sync_error=failure)
    return payload


def build_native_sessions_payload(
    sessions: list[Any],
    *,
    provider_name: str,
    source: str,
    sync_error: str,
    refresh_pending: bool,
    json_object: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    projected: list[dict[str, Any]] = []
    for session in sessions:
        item = json_object(session)
        item.setdefault("native_provider", provider_name)
        item.setdefault("native_session_source", source)
        item["presentation"] = build_native_session_presentation(item, sync_error=sync_error)
        projected.append(item)
    payload: dict[str, Any] = {
        "sessions": projected,
        "native_refresh_pending": refresh_pending,
        "native_session_source": source,
    }
    if sync_error:
        payload["native_sync_error"] = sync_error
    return payload
