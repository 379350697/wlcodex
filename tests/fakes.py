"""Shared fake helpers for tests."""

from __future__ import annotations

from typing import Any


def make_fake_telegram_context(
    user_id: int = 123,
    chat_id: int = 456,
    chat_type: str = "private",
) -> dict[str, Any]:
    return {
        "chat_id": chat_id,
        "user_id": user_id,
        "chat_type": chat_type,
    }
