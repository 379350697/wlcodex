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
