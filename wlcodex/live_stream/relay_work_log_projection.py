"""Read-only work-log projection composition for Relay task details."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def render_relay_work_log_body(
    detail: Any,
    *,
    hub: Any | None,
    canonical_payloads: dict[str, dict[str, Any]] | None,
    build_segments: Callable[..., list[Any]],
    render_segment: Callable[..., str],
    render_empty: Callable[[], str],
) -> str:
    """Render an already-read task detail without touching Relay lifecycle."""

    segments = build_segments(
        detail,
        hub=hub,
        canonical_payloads=canonical_payloads,
    )
    rows = [
        render_segment(segment, index=index)
        for index, segment in enumerate(segments)
    ]
    return "\n".join(rows) if rows else render_empty()
