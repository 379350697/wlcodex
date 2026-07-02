from __future__ import annotations

from typing import Any

from wlcodex.live_stream.native_templates.stable import render_stable_template
from wlcodex.live_stream.native_templates.timeline_v2 import render_timeline_v2_template


def render_native_template(
    provider: str,
    variant: str,
    context: dict[str, Any] | None = None,
) -> str:
    clean_provider = provider.strip() or "codex"
    clean_context = dict(context or {})
    if variant == "stable":
        return render_stable_template(clean_provider, clean_context)
    if variant == "timeline_v2":
        return render_timeline_v2_template(clean_provider, clean_context)
    raise ValueError(f"unknown native template variant: {variant}")
