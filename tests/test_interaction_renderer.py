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


# ---------------------------------------------------------------------------
# Typing lifecycle tests (Issue 1)
# ---------------------------------------------------------------------------


class CancellableTask:
    """Fake asyncio-style task that tracks cancellation."""
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class FakeTransportWithTypingTasks:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, object]] = []
        self.edited: list[tuple[int, int, str, object]] = []
        self.typing_tasks: list[CancellableTask] = []

    async def send(self, chat_id, text, buttons=None):
        self.sent.append((chat_id, text, buttons))
        return len(self.sent)

    async def edit(self, chat_id, message_id, text, buttons=None):
        self.edited.append((chat_id, message_id, text, buttons))

    async def typing(self, chat_id):
        task = CancellableTask()
        self.typing_tasks.append(task)
        return task


@pytest.mark.asyncio
async def test_typing_cancelled_on_run_completed() -> None:
    """typing task must be cancelled when run_completed arrives."""
    fake = FakeTransportWithTypingTasks()
    renderer = InteractionRenderer(
        transport=TelegramTransport(fake.send, fake.edit, fake.typing),
        profile=NaturalChatProfile(),
        min_interval_seconds=0.0,
    )

    await renderer.handle(InteractionEvent(event_type="run_started", chat_id=1, task_id=10))
    assert len(fake.typing_tasks) == 1
    assert not fake.typing_tasks[0].cancelled

    await renderer.handle(InteractionEvent(event_type="text_delta", chat_id=1, task_id=10, text="hi"))
    await renderer.handle(InteractionEvent(
        event_type="run_completed", chat_id=1, task_id=10, conversation_id=7,
    ))

    assert fake.typing_tasks[0].cancelled


@pytest.mark.asyncio
async def test_typing_cancelled_on_run_failed() -> None:
    """typing task must be cancelled when run_failed arrives."""
    fake = FakeTransportWithTypingTasks()
    renderer = InteractionRenderer(
        transport=TelegramTransport(fake.send, fake.edit, fake.typing),
        profile=NaturalChatProfile(),
        min_interval_seconds=0.0,
    )

    await renderer.handle(InteractionEvent(event_type="run_started", chat_id=1, task_id=11))
    assert len(fake.typing_tasks) == 1
    assert not fake.typing_tasks[0].cancelled

    await renderer.handle(InteractionEvent(
        event_type="run_failed", chat_id=1, task_id=11, text="boom",
    ))

    assert fake.typing_tasks[0].cancelled


@pytest.mark.asyncio
async def test_typing_cancelled_on_exception() -> None:
    """typing task must be cancelled even when handler raises."""
    fake = FakeTransportWithTypingTasks()

    class CrashingTransport:
        """Transport whose send raises on every call (not silenced by StreamingRenderer)."""
        def __init__(self, inner) -> None:
            self._inner = inner

        async def send(self, chat_id, text, buttons=None):
            raise RuntimeError("send failed")

        async def edit(self, chat_id, message_id, text, buttons=None):
            pass

        async def typing(self, chat_id):
            return await self._inner.typing(chat_id)

    renderer = InteractionRenderer(
        transport=CrashingTransport(fake),
        profile=NaturalChatProfile(),
        min_interval_seconds=0.0,
    )

    await renderer.handle(InteractionEvent(event_type="run_started", chat_id=1, task_id=10))
    assert len(fake.typing_tasks) == 1

    # text_delta will trigger StreamingRenderer.start -> send which raises
    # StreamingRenderer._flush catches Exception silently and falls back to send,
    # but start() calls send directly without try/except so it propagates
    try:
        await renderer.handle(InteractionEvent(event_type="text_delta", chat_id=1, task_id=10, text="hi"))
    except RuntimeError:
        pass

    assert fake.typing_tasks[0].cancelled


@pytest.mark.asyncio
async def test_typing_task_is_none_does_not_crash() -> None:
    """When typing() returns None, cancel must not crash."""
    fake = FakeTransport()  # typing returns None
    renderer = InteractionRenderer(
        transport=TelegramTransport(fake.send, fake.edit, fake.typing),
        profile=NaturalChatProfile(),
    )

    # Must not crash
    await renderer.handle(InteractionEvent(event_type="run_started", chat_id=1))
    await renderer.handle(InteractionEvent(event_type="run_completed", chat_id=1))


# --- Output manager integration tests ---


class FakePreviewTransport(TelegramTransport):
    """Transport that supports preview send/edit/body for output manager."""

    def __init__(self, send_fn, edit_fn, typing_fn):
        super().__init__(send_fn, edit_fn, typing_fn)
        self._preview_send_fn = send_fn
        self._preview_edit_fn = edit_fn
        self._body_send_fn = send_fn

    async def send_preview(self, chat_id, text):
        return await self._preview_send_fn(chat_id, text)

    async def edit_preview(self, chat_id, message_id, text, buttons=None):
        await self._preview_edit_fn(chat_id, message_id, text, buttons)

    async def send_body(self, chat_id, text, buttons=None):
        return await self._body_send_fn(chat_id, text, buttons)


@pytest.mark.asyncio
async def test_product_renderer_buffers_deltas_and_sends_final_once():
    from types import SimpleNamespace

    fake = FakeTransport()

    renderer = InteractionRenderer(
        transport=FakePreviewTransport(fake.send, fake.edit, fake.typing),
        profile=NaturalChatProfile(),
        min_interval_seconds=0.0,
        surface_resolver=lambda chat_id: "product",
        telegram_output_config=SimpleNamespace(
            semantic_min_chars=20,
            semantic_max_chars=80,
            final_chunk_chars=200,
            product_body_mode="final",
            terminal_body_mode="semantic_blocks",
            preview_send_timeout_seconds=2.0,
        ),
    )

    await renderer.handle(InteractionEvent(event_type="run_started", chat_id=1, conversation_id=7, task_id=10))
    await renderer.handle(InteractionEvent(event_type="text_delta", chat_id=1, conversation_id=7, task_id=10, text="第一段。"))
    await renderer.handle(InteractionEvent(event_type="text_delta", chat_id=1, conversation_id=7, task_id=10, text="第二段。"))

    body_messages_before_completion = [m for m in fake.sent if "第一段" in m[1]]
    assert body_messages_before_completion == []

    await renderer.handle(InteractionEvent(event_type="run_completed", chat_id=1, conversation_id=7, task_id=10))

    body_messages = [m for m in fake.sent if "第一段。第二段。" in m[1]]
    assert len(body_messages) == 1


@pytest.mark.asyncio
async def test_terminal_renderer_sends_semantic_blocks_while_running():
    from types import SimpleNamespace

    fake = FakeTransport()

    renderer = InteractionRenderer(
        transport=FakePreviewTransport(fake.send, fake.edit, fake.typing),
        profile=NaturalChatProfile(),
        min_interval_seconds=0.0,
        surface_resolver=lambda chat_id: "terminal",
        telegram_output_config=SimpleNamespace(
            semantic_min_chars=10,
            semantic_max_chars=30,
            final_chunk_chars=200,
            product_body_mode="final",
            terminal_body_mode="semantic_blocks",
            preview_send_timeout_seconds=2.0,
        ),
    )

    await renderer.handle(InteractionEvent(event_type="run_started", chat_id=1, conversation_id=7, task_id=10))
    await renderer.handle(InteractionEvent(event_type="text_delta", chat_id=1, conversation_id=7, task_id=10, text="第一段很长。\n\n第二段继续。"))

    assert any(message[1] == "第一段很长。" for message in fake.sent)
