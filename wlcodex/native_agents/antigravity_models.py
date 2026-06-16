from __future__ import annotations

from typing import Any


ANTIGRAVITY_OPUS_MODEL = "Claude Opus 4.6 (Thinking)"
ANTIGRAVITY_QUOTA_FALLBACK_MODEL = "Gemini 3.5 Flash (High)"
DEFAULT_ANTIGRAVITY_MODEL = ANTIGRAVITY_QUOTA_FALLBACK_MODEL

_ANTIGRAVITY_MODELS = (
    ANTIGRAVITY_OPUS_MODEL,
    "Claude Sonnet 4.6 (Thinking)",
    "Gemini 3.5 Flash (Medium)",
    ANTIGRAVITY_QUOTA_FALLBACK_MODEL,
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
