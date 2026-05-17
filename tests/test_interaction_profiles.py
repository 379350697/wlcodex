from wlcodex.interaction.buttons import natural_completion_buttons
from wlcodex.interaction.errors import classify_user_error


def test_natural_completion_buttons_are_small_and_deterministic() -> None:
    buttons = natural_completion_buttons(
        conversation_id=7,
        has_diff=True,
        include_new=True,
    )

    labels = [button["text"] for row in buttons for button in row]

    assert labels == ["继续", "查看 diff", "状态", "新对话"]
    assert all("callback_data" in button for row in buttons for button in row)


def test_natural_completion_buttons_hide_diff_when_none() -> None:
    buttons = natural_completion_buttons(
        conversation_id=7,
        has_diff=False,
        include_new=True,
    )

    labels = [button["text"] for row in buttons for button in row]

    assert "查看 diff" not in labels
    assert labels == ["继续", "状态", "新对话"]


def test_classify_user_error_hides_internal_exception_details() -> None:
    text = classify_user_error("Codex 启动失败：ConnectionError('token expired')")

    assert "ConnectionError" not in text
    assert "认证" in text or "登录" in text


from wlcodex.interaction.events import InteractionEvent
from wlcodex.interaction.profiles import (
    LegacyProfile,
    NaturalChatProfile,
    profile_from_name,
)


def test_profile_factory_accepts_known_profiles() -> None:
    assert isinstance(profile_from_name("natural"), NaturalChatProfile)
    assert isinstance(profile_from_name("legacy"), LegacyProfile)
    assert isinstance(profile_from_name("cockpit"), LegacyProfile)


def test_natural_profile_hides_started_message() -> None:
    profile = NaturalChatProfile()
    event = InteractionEvent(event_type="run_started", chat_id=1)

    assert profile.started_text(event) == ""


def test_legacy_profile_keeps_started_message() -> None:
    profile = LegacyProfile()
    event = InteractionEvent(event_type="run_started", chat_id=1, summary="正在处理")

    assert "正在处理" in profile.started_text(event)


def test_natural_profile_short_greeting() -> None:
    profile = NaturalChatProfile()

    assert profile.greeting_text() == "你好！直接说需要我看什么就行。"
