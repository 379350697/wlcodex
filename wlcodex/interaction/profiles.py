from __future__ import annotations

from dataclasses import dataclass

from wlcodex.interaction.buttons import natural_completion_buttons
from wlcodex.interaction.events import InteractionEvent
from wlcodex.interaction.errors import classify_user_error


@dataclass
class InteractionProfile:
    name: str

    def started_text(self, event: InteractionEvent) -> str:
        return event.summary

    def greeting_text(self) -> str:
        return "你好，我在。"

    def error_text(self, error: object) -> str:
        return classify_user_error(error)

    def completion_buttons(
        self, *, conversation_id: int | None, has_diff: bool = False
    ) -> list[list[dict[str, str]]]:
        return []


class NaturalChatProfile(InteractionProfile):
    def __init__(self) -> None:
        super().__init__(name="natural")

    def started_text(self, event: InteractionEvent) -> str:
        return ""

    def greeting_text(self) -> str:
        return "你好！直接说需要我看什么就行。"

    def completion_buttons(
        self, *, conversation_id: int | None, has_diff: bool = False
    ) -> list[list[dict[str, str]]]:
        if conversation_id is None:
            return []
        return natural_completion_buttons(
            conversation_id=conversation_id,
            has_diff=has_diff,
            include_new=True,
        )


class LegacyProfile(InteractionProfile):
    def __init__(self) -> None:
        super().__init__(name="legacy")

    def started_text(self, event: InteractionEvent) -> str:
        return event.summary or "正在处理你的消息，请稍候..."


def profile_from_name(name: str) -> InteractionProfile:
    normalized = name.strip().lower()
    if normalized == "natural":
        return NaturalChatProfile()
    if normalized in {"legacy", "cockpit"}:
        return LegacyProfile()
    raise ValueError(f"Unknown interaction profile: {name}")
