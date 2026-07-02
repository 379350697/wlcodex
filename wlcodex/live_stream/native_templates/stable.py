from __future__ import annotations

from typing import Any


def render_stable_template(
    provider: str,
    context: dict[str, Any],
) -> str:
    renderer = context.get("stable_renderer")
    if not callable(renderer):
        raise ValueError("stable template requires stable_renderer")
    theme = str(context.get("theme") or "")
    return str(renderer(provider, theme=theme))
