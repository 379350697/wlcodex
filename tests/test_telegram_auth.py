from wlcodex.telegram_app import is_authorized


def test_authorized_private_chat_user() -> None:
    assert is_authorized(user_id=123, chat_type="private", allowed_user_ids=frozenset({123}))


def test_rejects_group_chat() -> None:
    assert not is_authorized(user_id=123, chat_type="group", allowed_user_ids=frozenset({123}))


def test_rejects_unknown_user() -> None:
    assert not is_authorized(user_id=999, chat_type="private", allowed_user_ids=frozenset({123}))
