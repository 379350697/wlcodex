from wlcodex.interaction.events import InteractionEvent


def test_interaction_event_defaults_are_empty_and_safe() -> None:
    event = InteractionEvent(event_type="text_delta", chat_id=123, text="hello")

    assert event.event_type == "text_delta"
    assert event.chat_id == 123
    assert event.conversation_id is None
    assert event.task_id is None
    assert event.thread_id == ""
    assert event.text == "hello"
    assert event.buttons == []
    assert event.metadata == {}


def test_interaction_event_buttons_and_metadata_are_not_shared() -> None:
    first = InteractionEvent(event_type="run_completed", chat_id=1)
    second = InteractionEvent(event_type="run_completed", chat_id=1)

    first.buttons.append([{"text": "状态", "callback_data": "x"}])
    first.metadata["task"] = 7

    assert second.buttons == []
    assert second.metadata == {}
