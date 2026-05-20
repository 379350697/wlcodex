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
