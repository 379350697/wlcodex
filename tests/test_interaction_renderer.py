import pytest

from wlcodex.interaction.events import InteractionEvent
from wlcodex.interaction.profiles import NaturalChatProfile
from wlcodex.interaction.renderer import InteractionRenderer
from wlcodex.interaction.transport import TelegramTransport


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, object]] = []
        self.edited: list[tuple[int, int, str, object]] = []
        self.typing_count = 0

    async def send(self, chat_id, text, buttons=None):
        self.sent.append((chat_id, text, buttons))
        return len(self.sent)

    async def edit(self, chat_id, message_id, text, buttons=None):
        self.edited.append((chat_id, message_id, text, buttons))

    async def typing(self, chat_id):
        self.typing_count += 1
        return None


@pytest.mark.asyncio
async def test_natural_renderer_uses_typing_not_ack_on_started() -> None:
    fake = FakeTransport()
    renderer = InteractionRenderer(
        transport=TelegramTransport(fake.send, fake.edit, fake.typing),
        profile=NaturalChatProfile(),
    )

    await renderer.handle(InteractionEvent(event_type="run_started", chat_id=1))

    assert fake.typing_count == 1
    assert fake.sent == []


@pytest.mark.asyncio
async def test_natural_renderer_streams_delta_into_single_message() -> None:
    fake = FakeTransport()
    renderer = InteractionRenderer(
        transport=TelegramTransport(fake.send, fake.edit, fake.typing),
        profile=NaturalChatProfile(),
        min_interval_seconds=0.0,
    )

    await renderer.handle(InteractionEvent(event_type="text_delta", chat_id=1, task_id=10, text="hel"))
    await renderer.handle(InteractionEvent(event_type="text_delta", chat_id=1, task_id=10, text="lo"))

    assert fake.sent == [(1, "hel", None)]
    assert fake.edited[-1][2] == "hello"


@pytest.mark.asyncio
async def test_natural_renderer_flushes_buttons_on_completion() -> None:
    fake = FakeTransport()
    renderer = InteractionRenderer(
        transport=TelegramTransport(fake.send, fake.edit, fake.typing),
        profile=NaturalChatProfile(),
        min_interval_seconds=0.0,
    )

    await renderer.handle(
        InteractionEvent(
            event_type="text_delta",
            chat_id=1,
            conversation_id=7,
            task_id=10,
            text="done",
        )
    )
    await renderer.handle(
        InteractionEvent(
            event_type="run_completed",
            chat_id=1,
            conversation_id=7,
            task_id=10,
            metadata={"has_diff": True},
        )
    )

    assert fake.edited[-1][3] is not None
    labels = [button["text"] for row in fake.edited[-1][3] for button in row]
    assert "查看 diff" in labels
