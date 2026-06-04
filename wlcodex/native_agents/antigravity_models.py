from __future__ import annotations

from typing import Any


DEFAULT_ANTIGRAVITY_MODEL = "Claude Opus 4.6"

_ANTIGRAVITY_MODELS = (
    "Claude Opus 4.6",
    "Claude Sonnet 4.6",
    "Gemini 3.5",
    "Gemini 3.1",
    "GPT",
)


def antigravity_model_catalog(
    *,
    default_model: str = DEFAULT_ANTIGRAVITY_MODEL,
) -> list[dict[str, Any]]:
    return [
        {
            "id": model,
            "model": model,
            "displayName": model,
            "isDefault": model == default_model,
        }
        for model in _ANTIGRAVITY_MODELS
    ]
