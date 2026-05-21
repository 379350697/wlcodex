from types import SimpleNamespace

import pytest

from wlcodex.telegram_output import OutputRunKey, OutputSurface, TelegramOutputManager


class FakeTransport:
    def __init__(self):
        self.sent = []
        self.edited = []

    async def send_preview(self, chat_id, text):
        self.sent.append(("preview", chat_id, text, None))
        return 100

    async def edit_preview(self, chat_id, message_id, text, buttons=None):
        self.edited.append((chat_id, message_id, text, buttons))

    async def send_body(self, chat_id, text, buttons=None):
        self.sent.append(("body", chat_id, text, buttons))
        return len(self.sent)


class MissingPreviewTransport(FakeTransport):
    async def send_preview(self, chat_id, text):
        self.sent.append(("preview", chat_id, text, None))
        return -1


@pytest.mark.asyncio
async def test_product_buffers_body_until_completion():
    transport = FakeTransport()
    manager = TelegramOutputManager(
        transport=transport,
        semantic_min_chars=20,
        semantic_max_chars=80,
        final_chunk_chars=200,
    )
    key = OutputRunKey(chat_id=1, conversation_id=7, run_id="task-10")

    await manager.start(key, surface=OutputSurface.PRODUCT, text="Codex 正在处理")
    await manager.append_text(key, "第一段。")
    await manager.append_text(key, "第二段。")

    assert [item for item in transport.sent if item[0] == "body"] == []

    await manager.complete(key, buttons=[[{"text": "继续", "callback_data": "continue:7"}]])

    bodies = [item for item in transport.sent if item[0] == "body"]
    assert len(bodies) == 1
    assert bodies[0][2] == "第一段。第二段。"
    assert bodies[0][3] is not None
    assert key not in manager.sessions


@pytest.mark.asyncio
async def test_terminal_emits_semantic_blocks_while_running():
    transport = FakeTransport()
    manager = TelegramOutputManager(
        transport=transport,
        semantic_min_chars=10,
        semantic_max_chars=30,
        final_chunk_chars=200,
    )
    key = OutputRunKey(chat_id=1, conversation_id=7, run_id="task-10")

    await manager.start(key, surface=OutputSurface.TERMINAL, text="Codex 正在处理")
    await manager.append_text(key, "第一段很长。\n\n第二段继续。")

    bodies = [item for item in transport.sent if item[0] == "body"]
    assert bodies == [("body", 1, "第一段很长。", None)]


@pytest.mark.asyncio
async def test_output_sessions_do_not_mix_runs():
    transport = FakeTransport()
    manager = TelegramOutputManager(transport=transport)
    old_key = OutputRunKey(chat_id=1, conversation_id=7, run_id="task-old")
    new_key = OutputRunKey(chat_id=1, conversation_id=7, run_id="task-new")

    await manager.start(old_key, surface=OutputSurface.PRODUCT, text="旧任务")
    await manager.append_text(old_key, "旧输出")
    await manager.start(new_key, surface=OutputSurface.PRODUCT, text="新任务")
    await manager.append_text(new_key, "新输出")
    await manager.complete(new_key)
    await manager.complete(old_key)

    body_texts = [item[2] for item in transport.sent if item[0] == "body"]
    assert "新输出" in body_texts
    assert "旧输出" in body_texts
    assert "旧输出新输出" not in body_texts


@pytest.mark.asyncio
async def test_body_modes_are_driven_by_surface_policy():
    transport = FakeTransport()
    manager = TelegramOutputManager(
        transport=transport,
        semantic_min_chars=10,
        semantic_max_chars=30,
        final_chunk_chars=200,
        product_body_mode="semantic_blocks",
        terminal_body_mode="final",
        terminal_block_idle_seconds=0,
    )
    product_key = OutputRunKey(chat_id=1, conversation_id=7, run_id="product")
    terminal_key = OutputRunKey(chat_id=1, conversation_id=7, run_id="terminal")

    await manager.start(product_key, surface=OutputSurface.PRODUCT, text="产品任务")
    await manager.append_text(product_key, "第一段很长。\n\n第二段继续。")
    await manager.start(terminal_key, surface=OutputSurface.TERMINAL, text="终端任务")
    await manager.append_text(terminal_key, "终端第一段很长。\n\n终端第二段继续。")

    body_texts = [item[2] for item in transport.sent if item[0] == "body"]
    assert "第一段很长。" in body_texts
    assert not any("终端第一段" in text for text in body_texts)

    await manager.complete(terminal_key)
    body_texts = [item[2] for item in transport.sent if item[0] == "body"]
    assert any("终端第一段" in text for text in body_texts)


@pytest.mark.asyncio
async def test_preview_status_updates_are_deduped_and_throttled():
    now = 100.0

    def time_fn():
        return now

    transport = FakeTransport()
    manager = TelegramOutputManager(
        transport=transport,
        preview_edit_min_interval_seconds=2.0,
        time_fn=time_fn,
    )
    key = OutputRunKey(chat_id=1, conversation_id=7, run_id="task-10")

    await manager.start(key, surface=OutputSurface.PRODUCT, text="正在处理")
    await manager.update_status(key, "正在处理")
    await manager.update_status(key, "Codex 正在拆解需求")

    assert transport.edited == []

    now = 103.0
    await manager.update_status(key, "Codex 正在拆解需求")

    assert transport.edited == [(1, 100, "Codex 正在拆解需求", None)]


@pytest.mark.asyncio
async def test_terminal_idle_flush_uses_configured_delay():
    transport = FakeTransport()
    manager = TelegramOutputManager(
        transport=transport,
        semantic_min_chars=100,
        semantic_max_chars=200,
        terminal_block_idle_seconds=0.01,
    )
    key = OutputRunKey(chat_id=1, conversation_id=7, run_id="task-10")

    await manager.start(key, surface=OutputSurface.TERMINAL, text="终端任务")
    # Must be long enough to pass idle readability guard (>=120 chars)
    # and end at a sentence boundary.
    long_text = "这是一段足够长的测试文本用来验证空闲刷新功能是否可以正确发送消息。" * 5
    await manager.append_text(key, long_text)
    await manager.wait_for_idle_flush(key)

    body_texts = [item[2] for item in transport.sent if item[0] == "body"]
    assert any("测试文本" in t for t in body_texts)


@pytest.mark.asyncio
async def test_failure_falls_back_to_body_when_preview_message_id_missing():
    transport = MissingPreviewTransport()
    manager = TelegramOutputManager(transport=transport)
    key = OutputRunKey(chat_id=1, conversation_id=7, run_id="task-10")

    await manager.start(key, surface=OutputSurface.PRODUCT, text="正在处理")
    await manager.fail(key, error_summary="网络异常")

    bodies = [item for item in transport.sent if item[0] == "body"]
    assert bodies == [("body", 1, "运行失败: 网络异常", None)]


@pytest.mark.asyncio
async def test_interrupt_falls_back_to_body_when_preview_message_id_missing():
    transport = MissingPreviewTransport()
    manager = TelegramOutputManager(transport=transport)
    key = OutputRunKey(chat_id=1, conversation_id=7, run_id="task-10")

    await manager.start(key, surface=OutputSurface.PRODUCT, text="正在处理")
    await manager.interrupt(key)

    bodies = [item for item in transport.sent if item[0] == "body"]
    assert bodies == [("body", 1, "已打断", None)]


@pytest.mark.asyncio
async def test_empty_completion_falls_back_to_body_when_preview_message_id_missing():
    transport = MissingPreviewTransport()
    manager = TelegramOutputManager(transport=transport)
    key = OutputRunKey(chat_id=1, conversation_id=7, run_id="task-10")

    await manager.start(key, surface=OutputSurface.PRODUCT, text="正在处理")
    await manager.complete(key)

    bodies = [item for item in transport.sent if item[0] == "body"]
    assert bodies == [("body", 1, "运行完成", None)]


# ---------------------------------------------------------------------------
# Idle flush readability guard tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_flush_does_not_send_short_fragments():
    """Idle flush must not send single words or half-sentences."""
    transport = FakeTransport()
    manager = TelegramOutputManager(
        transport=transport,
        semantic_min_chars=900,
        semantic_max_chars=3200,
        terminal_block_idle_seconds=0.01,
    )
    key = OutputRunKey(chat_id=1, conversation_id=7, run_id="task-10")

    await manager.start(key, surface=OutputSurface.TERMINAL, text="终端任务")
    # Short text below idle minimum (120 chars) — should NOT be sent
    await manager.append_text(key, "短句而已。")
    await manager.wait_for_idle_flush(key)

    body_texts = [item[2] for item in transport.sent if item[0] == "body"]
    assert body_texts == []


@pytest.mark.asyncio
async def test_idle_flush_requires_sentence_boundary():
    """Idle flush must only send when buffer ends at a sentence boundary."""
    transport = FakeTransport()
    manager = TelegramOutputManager(
        transport=transport,
        semantic_min_chars=900,
        semantic_max_chars=3200,
        terminal_block_idle_seconds=0.01,
    )
    key = OutputRunKey(chat_id=1, conversation_id=7, run_id="task-10")

    await manager.start(key, surface=OutputSurface.TERMINAL, text="终端任务")
    # Long enough (>120 chars) but no sentence ending — should NOT flush
    long_no_period = "这是足够长的文本但是没有句号结尾所以不应该被空闲刷新发送出去这是足够了" * 3
    await manager.append_text(key, long_no_period)
    await manager.wait_for_idle_flush(key)

    body_texts = [item[2] for item in transport.sent if item[0] == "body"]
    assert body_texts == []


@pytest.mark.asyncio
async def test_idle_flush_sends_at_sentence_boundary():
    """Idle flush sends when buffer is long enough AND ends with period."""
    transport = FakeTransport()
    manager = TelegramOutputManager(
        transport=transport,
        semantic_min_chars=900,
        semantic_max_chars=3200,
        terminal_block_idle_seconds=0.01,
    )
    key = OutputRunKey(chat_id=1, conversation_id=7, run_id="task-10")

    await manager.start(key, surface=OutputSurface.TERMINAL, text="终端任务")
    # Long enough (>120 chars) AND ends with Chinese period — should flush
    long_with_period = "这是足够长的测试文本用来验证空闲刷新功能是否可以正确发送。" * 6
    await manager.append_text(key, long_with_period)
    await manager.wait_for_idle_flush(key)

    body_texts = [item[2] for item in transport.sent if item[0] == "body"]
    assert any("测试文本" in t for t in body_texts)


@pytest.mark.asyncio
async def test_final_completion_flushes_even_short_text():
    """Completion always flushes everything regardless of idle guards."""
    transport = FakeTransport()
    manager = TelegramOutputManager(
        transport=transport,
        semantic_min_chars=900,
        semantic_max_chars=3200,
        final_chunk_chars=200,
        terminal_block_idle_seconds=5.0,  # long idle delay
    )
    key = OutputRunKey(chat_id=1, conversation_id=7, run_id="task-10")

    await manager.start(key, surface=OutputSurface.TERMINAL, text="终端任务")
    # Short text that would NOT pass idle guard
    await manager.append_text(key, "短句。")
    # Complete always flushes
    await manager.complete(key)

    body_texts = [item[2] for item in transport.sent if item[0] == "body"]
    assert any("短句。" in t for t in body_texts)


@pytest.mark.asyncio
async def test_idle_flush_sends_at_english_period_boundary():
    """Idle flush sends when buffer ends with English period."""
    transport = FakeTransport()
    manager = TelegramOutputManager(
        transport=transport,
        semantic_min_chars=900,
        semantic_max_chars=3200,
        terminal_block_idle_seconds=0.01,
    )
    key = OutputRunKey(chat_id=1, conversation_id=7, run_id="task-10")

    await manager.start(key, surface=OutputSurface.TERMINAL, text="terminal task")
    long_english = (
        "This is a sufficiently long text that ends with a proper English period. "
        * 8
    )
    await manager.append_text(key, long_english)
    await manager.wait_for_idle_flush(key)

    body_texts = [item[2] for item in transport.sent if item[0] == "body"]
    assert any("English period" in t for t in body_texts)


@pytest.mark.asyncio
async def test_idle_flush_sends_at_paragraph_break():
    """Idle flush sends when buffer ends with double newline (paragraph break)."""
    transport = FakeTransport()
    manager = TelegramOutputManager(
        transport=transport,
        semantic_min_chars=900,
        semantic_max_chars=3200,
        terminal_block_idle_seconds=0.01,
    )
    key = OutputRunKey(chat_id=1, conversation_id=7, run_id="task-10")

    await manager.start(key, surface=OutputSurface.TERMINAL, text="terminal task")
    paragraph_text = "这是段落内容。" * 20 + "\n\n"
    await manager.append_text(key, paragraph_text)
    await manager.wait_for_idle_flush(key)

    body_texts = [item[2] for item in transport.sent if item[0] == "body"]
    assert any("段落内容" in t for t in body_texts)


@pytest.mark.asyncio
async def test_idle_flush_sends_at_list_newline_boundary():
    """Idle flush sends when buffer has enough content and ends with newline (list boundary)."""
    transport = FakeTransport()
    manager = TelegramOutputManager(
        transport=transport,
        semantic_min_chars=900,
        semantic_max_chars=3200,
        terminal_block_idle_seconds=0.01,
    )
    key = OutputRunKey(chat_id=1, conversation_id=7, run_id="task-10")

    await manager.start(key, surface=OutputSurface.TERMINAL, text="terminal task")
    list_text = "列表项内容。" * 35 + "\n"
    await manager.append_text(key, list_text)
    await manager.wait_for_idle_flush(key)

    body_texts = [item[2] for item in transport.sent if item[0] == "body"]
    assert any("列表项内容" in t for t in body_texts)
