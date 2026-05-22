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


@pytest.mark.asyncio
async def test_runtime_progress_starts_preview_session_without_run_started():
    from types import SimpleNamespace

    from wlcodex.interaction.runtime_renderer import RuntimeRunState

    fake = FakeTransport()

    renderer = InteractionRenderer(
        transport=FakePreviewTransport(fake.send, fake.edit, fake.typing),
        profile=NaturalChatProfile(),
        min_interval_seconds=0.0,
        surface_resolver=lambda chat_id: "product",
        telegram_output_config=SimpleNamespace(
            preview_enabled=True,
            preview_edit_min_interval_seconds=0.0,
            semantic_min_chars=20,
            semantic_max_chars=80,
            final_chunk_chars=200,
            product_body_mode="final",
            terminal_body_mode="semantic_blocks",
            preview_send_timeout_seconds=2.0,
        ),
    )

    await renderer.handle(
        InteractionEvent(
            event_type="runtime_progress",
            chat_id=1,
            conversation_id=7,
            task_id=10,
            metadata={
                "runtime_state": RuntimeRunState(
                    phase="running_analysis",
                    active_agent="codex",
                    agent_status="running",
                )
            },
        )
    )

    assert fake.sent
    # Cockpit renderer shows phase with agent name
    assert "Codex" in fake.sent[0][1] or "分析" in fake.sent[0][1]
    from types import SimpleNamespace

    from wlcodex.interaction.runtime_renderer import RuntimeRunState

    fake = FakeTransport()

    renderer = InteractionRenderer(
        transport=FakePreviewTransport(fake.send, fake.edit, fake.typing),
        profile=NaturalChatProfile(),
        min_interval_seconds=0.0,
        surface_resolver=lambda chat_id: "product",
        telegram_output_config=SimpleNamespace(
            preview_enabled=True,
            preview_edit_min_interval_seconds=0.0,
            semantic_min_chars=20,
            semantic_max_chars=80,
            final_chunk_chars=200,
            product_body_mode="final",
            terminal_body_mode="semantic_blocks",
            preview_send_timeout_seconds=2.0,
        ),
    )
    state = RuntimeRunState(
        phase="running_verification",
        active_agent="codex",
        agent_status="running",
    )
    state.current_command = "pytest tests/ -q"
    state.elapsed_seconds = 200
    state.estimated_remaining = "约3-5分钟"

    await renderer.handle(
        InteractionEvent(
            event_type="runtime_progress",
            chat_id=1,
            conversation_id=7,
            task_id=10,
            metadata={"runtime_state": state},
        )
    )

    assert fake.sent
    assert "Codex正在执行：pytest tests/ -q" in fake.sent[0][1]
    assert "已运行：3m20s" in fake.sent[0][1]
    assert "预计还需：约3-5分钟" in fake.sent[0][1]


@pytest.mark.asyncio
async def test_runtime_heartbeat_edits_preview_with_activity_hint():
    from types import SimpleNamespace

    from wlcodex.interaction.runtime_renderer import RuntimeRunState

    fake = FakeTransport()

    renderer = InteractionRenderer(
        transport=FakePreviewTransport(fake.send, fake.edit, fake.typing),
        profile=NaturalChatProfile(),
        min_interval_seconds=0.0,
        surface_resolver=lambda chat_id: "product",
        telegram_output_config=SimpleNamespace(
            preview_enabled=True,
            preview_edit_min_interval_seconds=0.0,
            semantic_min_chars=20,
            semantic_max_chars=80,
            final_chunk_chars=200,
            product_body_mode="final",
            terminal_body_mode="semantic_blocks",
            preview_send_timeout_seconds=2.0,
        ),
    )

    await renderer.handle(
        InteractionEvent(
            event_type="runtime_progress",
            chat_id=1,
            conversation_id=7,
            task_id=10,
            metadata={
                "runtime_state": RuntimeRunState(
                    phase="running_verification",
                    active_agent="codex",
                    agent_status="running",
                )
            },
        )
    )
    await renderer.handle(
        InteractionEvent(
            event_type="runtime_heartbeat",
            chat_id=1,
            conversation_id=7,
            task_id=10,
            metadata={
                "runtime_state": RuntimeRunState(
                    phase="running_verification",
                    active_agent="codex",
                    agent_status="running",
                    last_activity_at="2026-05-22T01:00:00+08:00",
                )
            },
        )
    )

    assert any("还在执行" in edit[2] for edit in fake.edited)


@pytest.mark.asyncio
async def test_interrupt_closes_old_output_session_before_new_run():
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
    await renderer.handle(InteractionEvent(event_type="text_delta", chat_id=1, conversation_id=7, task_id=10, text="旧输出"))
    await renderer.handle(InteractionEvent(event_type="run_failed", chat_id=1, conversation_id=7, task_id=10, text="interrupted", metadata={"runtime_state": "cancelled"}))
    await renderer.handle(InteractionEvent(event_type="run_started", chat_id=1, conversation_id=7, task_id=11))
    await renderer.handle(InteractionEvent(event_type="text_delta", chat_id=1, conversation_id=7, task_id=11, text="新输出"))
    await renderer.handle(InteractionEvent(event_type="run_completed", chat_id=1, conversation_id=7, task_id=11))

    assert any("已打断" in edit[2] for edit in fake.edited)
    assert any("新输出" in sent[1] for sent in fake.sent)
    assert not any("旧输出新输出" in sent[1] for sent in fake.sent)


def test_renderer_status_surface_owner_tracks_preview_configuration() -> None:
    from types import SimpleNamespace

    fake = FakeTransport()
    renderer = InteractionRenderer(
        transport=FakePreviewTransport(fake.send, fake.edit, fake.typing),
        profile=NaturalChatProfile(),
        surface_resolver=lambda chat_id: "product",
        telegram_output_config=SimpleNamespace(preview_enabled=True),
    )
    assert renderer.has_runtime_status_surface() is True

    disabled = InteractionRenderer(
        transport=FakePreviewTransport(fake.send, fake.edit, fake.typing),
        profile=NaturalChatProfile(),
        surface_resolver=lambda chat_id: "product",
        telegram_output_config=SimpleNamespace(preview_enabled=False),
    )
    assert disabled.has_runtime_status_surface() is False
    disabled._runtime_progress = object()
    assert disabled.has_runtime_status_surface() is True


@pytest.mark.asyncio
async def test_preview_disabled_keeps_body_policy_without_preview_message():
    from types import SimpleNamespace

    fake = FakeTransport()
    renderer = InteractionRenderer(
        transport=FakePreviewTransport(fake.send, fake.edit, fake.typing),
        profile=NaturalChatProfile(),
        min_interval_seconds=0.0,
        surface_resolver=lambda chat_id: "product",
        telegram_output_config=SimpleNamespace(
            preview_enabled=False,
            semantic_min_chars=20,
            semantic_max_chars=80,
            final_chunk_chars=200,
            product_body_mode="final",
            terminal_body_mode="semantic_blocks",
            preview_send_timeout_seconds=2.0,
        ),
    )

    await renderer.handle(InteractionEvent(event_type="run_started", chat_id=1, conversation_id=7, task_id=10))
    await renderer.handle(InteractionEvent(event_type="text_delta", chat_id=1, conversation_id=7, task_id=10, text="最终正文。"))
    assert fake.sent == []
    assert fake.typing_count == 1

    await renderer.handle(InteractionEvent(event_type="run_completed", chat_id=1, conversation_id=7, task_id=10))

    assert any(message[1] == "最终正文。" for message in fake.sent)


@pytest.mark.asyncio
async def test_runtime_final_closes_output_session_and_flushes_body():
    from types import SimpleNamespace
    from wlcodex.interaction.runtime_renderer import RuntimeRunState

    fake = FakeTransport()
    renderer = InteractionRenderer(
        transport=FakePreviewTransport(fake.send, fake.edit, fake.typing),
        profile=NaturalChatProfile(),
        min_interval_seconds=0.0,
        surface_resolver=lambda chat_id: "product",
        telegram_output_config=SimpleNamespace(
            preview_enabled=True,
            semantic_min_chars=20,
            semantic_max_chars=80,
            final_chunk_chars=200,
            product_body_mode="final",
            terminal_body_mode="semantic_blocks",
            preview_send_timeout_seconds=2.0,
        ),
    )

    await renderer.handle(InteractionEvent(event_type="run_started", chat_id=1, conversation_id=7, task_id=10))
    await renderer.handle(InteractionEvent(event_type="text_delta", chat_id=1, conversation_id=7, task_id=10, text="最终正文。"))
    await renderer.handle(
        InteractionEvent(
            event_type="runtime_final",
            chat_id=1,
            conversation_id=7,
            task_id=10,
            metadata={"runtime_state": RuntimeRunState(phase="completed", is_terminal=True)},
        )
    )

    assert any(message[1] == "最终正文。" for message in fake.sent)
    assert any(edit[2] == "运行完成" for edit in fake.edited)


@pytest.mark.asyncio
async def test_run_failed_treats_runtime_state_cancelled_as_interrupt():
    from types import SimpleNamespace
    from wlcodex.interaction.runtime_renderer import RuntimeRunState

    fake = FakeTransport()
    renderer = InteractionRenderer(
        transport=FakePreviewTransport(fake.send, fake.edit, fake.typing),
        profile=NaturalChatProfile(),
        min_interval_seconds=0.0,
        surface_resolver=lambda chat_id: "product",
        telegram_output_config=SimpleNamespace(
            preview_enabled=True,
            semantic_min_chars=20,
            semantic_max_chars=80,
            final_chunk_chars=200,
            product_body_mode="final",
            terminal_body_mode="semantic_blocks",
            preview_send_timeout_seconds=2.0,
        ),
    )

    await renderer.handle(InteractionEvent(event_type="run_started", chat_id=1, conversation_id=7, task_id=10))
    await renderer.handle(
        InteractionEvent(
            event_type="run_failed",
            chat_id=1,
            conversation_id=7,
            task_id=10,
            metadata={"runtime_state": RuntimeRunState(phase="cancelled")},
        )
    )

    assert any(edit[2] == "已打断" for edit in fake.edited)


# ── SurfacePolicy integration ────────────────────────────────────────────────


def test_interaction_renderer_accepts_surface_policy():
    """InteractionRenderer must accept surface_policy and use it for output params."""
    from types import SimpleNamespace
    from wlcodex.surfaces.core.models import SurfacePolicy, TerminalPolicy, ProductPolicy

    policy = SurfacePolicy(
        terminal=TerminalPolicy(max_frame_chars=3500, redaction_enabled=True),
        product=ProductPolicy(
            body_mode="final",
            semantic_min_chars=800,
            semantic_max_chars=3000,
            final_chunk_chars=4000,
        ),
    )
    fake = FakeTransport()
    renderer = InteractionRenderer(
        transport=fake,
        profile=NaturalChatProfile(),
        min_interval_seconds=0.0,
        surface_policy=policy,
    )
    # Verify output manager was created with policy-derived config
    assert renderer._output_manager is not None
    assert renderer._output_manager._policy.min_chars == 800
    assert renderer._output_manager._policy.max_chars == 3000
    assert renderer._output_manager._policy.final_max_chars == 4000


def test_interaction_renderer_falls_back_to_telegram_output_config():
    """When surface_policy is not provided, fall back to telegram_output_config."""
    from types import SimpleNamespace

    fake = FakeTransport()
    renderer = InteractionRenderer(
        transport=fake,
        profile=NaturalChatProfile(),
        min_interval_seconds=0.0,
        telegram_output_config=SimpleNamespace(
            semantic_min_chars=777,
            semantic_max_chars=2222,
            final_chunk_chars=1111,
            preview_enabled=True,
            preview_edit_min_interval_seconds=2.0,
            product_body_mode="final",
            terminal_body_mode="semantic_blocks",
            terminal_block_idle_seconds=2.0,
        ),
    )
    assert renderer._output_manager._policy.min_chars == 777
