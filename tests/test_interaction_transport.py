import pytest

from wlcodex.interaction.transport import TelegramTransport


@pytest.mark.asyncio
async def test_transport_delegates_send_edit_and_typing() -> None:
    calls: list[tuple[str, object]] = []

    async def send(chat_id, text, buttons=None):
        calls.append(("send", (chat_id, text, buttons)))
        return 99

    async def edit(chat_id, message_id, text, buttons=None):
        calls.append(("edit", (chat_id, message_id, text, buttons)))

    async def typing(chat_id):
        calls.append(("typing", chat_id))
        return "typing-task"

    transport = TelegramTransport(send, edit, typing)

    message_id = await transport.send(1, "hello", [[{"text": "状态", "callback_data": "s"}]])
    await transport.edit(1, message_id, "updated")
    typing_task = await transport.typing(1)

    assert message_id == 99
    assert typing_task == "typing-task"
    assert calls[0][0] == "send"
    assert calls[1][0] == "edit"
    assert calls[2] == ("typing", 1)


@pytest.mark.asyncio
async def test_transport_callback_answer_is_optional() -> None:
    async def send(chat_id, text, buttons=None):
        return 1

    async def edit(chat_id, message_id, text, buttons=None):
        return None

    async def typing(chat_id):
        return None

    transport = TelegramTransport(send, edit, typing)

    await transport.answer_callback("ignored")
