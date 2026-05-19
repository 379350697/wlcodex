"""Tests for streaming renderer."""

import pytest
from wlcodex.streaming import StreamingRenderer


class FakeClock:
    def __init__(self) -> None:
        self._time = 0.0

    def time(self) -> float:
        return self._time

    def advance(self, seconds: float) -> None:
        self._time += seconds


class FakeBot:
    def __init__(self) -> None:
        self.edit_count = 0
        self.send_count = 0
        self.last_text = ""
        self.messages: list[tuple[int, str]] = []

    async def send(self, chat_id: int, text: str) -> int:
        self.send_count += 1
        self.last_text = text
        msg_id = self.send_count
        self.messages.append((chat_id, text))
        return msg_id

    async def edit(self, chat_id: int, message_id: int, text: str, buttons=None) -> None:
        self.edit_count += 1
        self.last_text = text


@pytest.mark.asyncio
async def test_streaming_renderer_throttles_edits() -> None:
    fake_bot = FakeBot()
    clock = FakeClock()
    renderer = StreamingRenderer(
        fake_bot.send, fake_bot.edit, min_interval_seconds=1.0, clock=clock
    )

    await renderer.start(chat_id=1, initial_text="hello")
    assert fake_bot.send_count == 1

    await renderer.append("a")
    await renderer.append("b")
    # Within same time, should batch into one edit
    assert fake_bot.edit_count <= 1

    clock.advance(1.1)
    await renderer.append("c")
    assert fake_bot.edit_count >= 1


@pytest.mark.asyncio
async def test_streaming_renderer_finish_flushes() -> None:
    fake_bot = FakeBot()
    clock = FakeClock()
    renderer = StreamingRenderer(
        fake_bot.send, fake_bot.edit, min_interval_seconds=1.0, clock=clock
    )

    await renderer.start(chat_id=1)
    await renderer.append("streaming text")
    await renderer.finish()

    assert "streaming text" in fake_bot.last_text


@pytest.mark.asyncio
async def test_streaming_renderer_empty_start() -> None:
    fake_bot = FakeBot()
    clock = FakeClock()
    renderer = StreamingRenderer(
        fake_bot.send, fake_bot.edit, min_interval_seconds=1.0, clock=clock
    )

    await renderer.start(chat_id=1)
    assert fake_bot.send_count == 0  # No initial text, no send

    await renderer.append("first text")
    assert "first text" in fake_bot.last_text


@pytest.mark.asyncio
async def test_streaming_renderer_does_not_edit_placeholder_message_id() -> None:
    class OutboxLikeBot(FakeBot):
        async def send(self, chat_id: int, text: str) -> int:
            self.send_count += 1
            self.last_text = text
            self.messages.append((chat_id, text))
            return -1

        async def edit(
            self, chat_id: int, message_id: int, text: str, buttons=None
        ) -> None:
            self.edit_count += 1
            raise AssertionError("placeholder message id must not be edited")

    fake_bot = OutboxLikeBot()
    renderer = StreamingRenderer(
        fake_bot.send, fake_bot.edit, min_interval_seconds=0.0,
    )

    await renderer.start(chat_id=1, initial_text="hello")
    await renderer.append(" world")
    await renderer.finish()

    assert fake_bot.edit_count == 0
    assert fake_bot.send_count >= 2


@pytest.mark.asyncio
async def test_streaming_renderer_sends_only_new_text_without_editable_message_id() -> None:
    class OutboxLikeBot(FakeBot):
        async def send(self, chat_id: int, text: str) -> int:
            self.send_count += 1
            self.last_text = text
            self.messages.append((chat_id, text))
            return -1

        async def edit(
            self, chat_id: int, message_id: int, text: str, buttons=None
        ) -> None:
            raise AssertionError("placeholder message id must not be edited")

    fake_bot = OutboxLikeBot()
    renderer = StreamingRenderer(
        fake_bot.send, fake_bot.edit, min_interval_seconds=0.0,
    )

    await renderer.start(chat_id=1)
    await renderer.append("first")
    await renderer.append("\nsecond")
    await renderer.append("\nthird")

    assert [message for _, message in fake_bot.messages] == [
        "first",
        "\nsecond",
        "\nthird",
    ]


@pytest.mark.asyncio
async def test_streaming_renderer_caps_telegram_text() -> None:
    fake_bot = FakeBot()
    renderer = StreamingRenderer(
        fake_bot.send,
        fake_bot.edit,
        min_interval_seconds=0.0,
        max_text_length=40,
    )

    await renderer.start(chat_id=1)
    await renderer.append("x" * 100)

    assert len(fake_bot.last_text) <= 40
    assert "已截断" in fake_bot.last_text
