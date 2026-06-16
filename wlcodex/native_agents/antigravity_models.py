from __future__ import annotations

from typing import Any


DEFAULT_ANTIGRAVITY_MODEL = "Claude Opus 4.6 (Thinking)"
ANTIGRAVITY_QUOTA_FALLBACK_MODEL = "Gemini 3.5 Flash (High)"

_ANTIGRAVITY_MODELS = (
    "Claude Opus 4.6 (Thinking)",
    "Claude Sonnet 4.6 (Thinking)",
    "Gemini 3.5 Flash (Medium)",
    "Gemini 3.5 Flash (High)",
    "Gemini 3.5 Flash (Low)",
    "Gemini 3.1 Pro (High)",
    "Gemini 3.1 Pro (Low)",
    "GPT-OSS 120B (Medium)",
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
