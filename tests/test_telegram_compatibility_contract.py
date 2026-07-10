"""Contracts for Telegram's historical-compatibility entry point."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from wlcodex.presentation_contract import telegram_compatibility_presentation
from wlcodex.telegram_app import WlCodexHandlers, _telegram_button


class _Bot:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send_message(self, **kwargs: object) -> SimpleNamespace:
        self.messages.append(kwargs)
        return SimpleNamespace(message_id=len(self.messages))

    async def send_chat_action(self, **kwargs: object) -> None:
        return None


class _Ledger:
    def __init__(
        self,
        active: object | None = None,
        archived: tuple[object, ...] = (),
    ) -> None:
        self.active = active
        self.archived = archived
        self.create_calls = 0

    def get_active_conversation(self, chat_id: int) -> object | None:
        return self.active

    def list_conversations_by_chat(
        self, chat_id: int, *, include_archived: bool = False
    ) -> tuple[object, ...]:
        return self.archived if include_archived else ()

    def record_telegram_update(self, **kwargs: object) -> None:
        return None

    def create_conversation(self, **kwargs: object) -> None:
        self.create_calls += 1
        raise AssertionError("new Telegram traffic must not create a legacy conversation")


class _Controller:
    def __init__(self) -> None:
        self.command_calls: list[str] = []
        self.text_calls: list[str] = []

    async def handle(self, text: str, context: object) -> SimpleNamespace:
        self.command_calls.append(text)
        return SimpleNamespace(text="legacy command", buttons=None, already_rendered=False)

    async def handle_conversation_text(self, text: str, context: object) -> SimpleNamespace:
        self.text_calls.append(text)
        return SimpleNamespace(text="legacy text", buttons=None, already_rendered=False)


def _update(text: str, *, chat_id: int = 41) -> SimpleNamespace:
    return SimpleNamespace(
        update_id=9,
        effective_user=SimpleNamespace(id=7),
        effective_chat=SimpleNamespace(id=chat_id, type="private"),
        effective_message=SimpleNamespace(text=text),
        callback_query=None,
    )


def _handlers(ledger: _Ledger, controller: _Controller, bot: _Bot) -> WlCodexHandlers:
    return WlCodexHandlers(
        config=SimpleNamespace(
            telegram=SimpleNamespace(allowed_user_ids=frozenset({7})),
            interaction=SimpleNamespace(profile="natural"),
        ),
        controller=controller,
        ledger=ledger,
        approval_service=object(),
        bot=bot,
    )


def _button_actions(bot: _Bot) -> list[tuple[str | None, str | None]]:
    markup = bot.messages[-1]["reply_markup"]
    return [
        (button.url, button.callback_data)
        for row in markup.inline_keyboard
        for button in row
    ]


def test_telegram_compatibility_projection_is_the_shared_pure_contract() -> None:
    actions = ["open_native", "open_relay"]

    presentation = telegram_compatibility_presentation(
        legacy_compatible=False,
        next_action="打开 Native 或 Relay。",
        allowed_actions=actions,
    )

    # Telegram has no authority to infer an old Workbench's live lifecycle.
    # It exposes the same shape as Relay, identifies the stale source, and
    # does not retain mutable input owned by the caller.
    assert presentation == {
        "state": "stale",
        "freshness": {
            "source": "telegram_redirect",
            "updated_at": "",
            "is_stale": True,
            "reason": "Telegram 不再创建新会话或维护旧主状态，请转到 Native 或 Relay。",
        },
        "current_actor": {"role": "", "label": "", "status": ""},
        "blocking_reason": "Telegram 不再创建新会话或维护旧主状态，请转到 Native 或 Relay。",
        "next_action": "打开 Native 或 Relay。",
        "allowed_actions": ["open_native", "open_relay"],
    }
    actions.append("mutate_caller")
    assert presentation["allowed_actions"] == ["open_native", "open_relay"]


@pytest.mark.asyncio
async def test_telegram_compatibility_prompts_do_not_touch_legacy_state() -> None:
    ledger = _Ledger()
    controller = _Controller()
    bot = _Bot()
    handlers = _handlers(ledger, controller, bot)

    # These are the helpers used by redirect commands and legacy verification
    # callbacks.  They only render a value projection and send a Telegram
    # message; they must not create/archive/reconcile a conversation.
    await handlers._redirect_to_primary_surface(41)
    await handlers._send_surface_jump(41, "native")
    await handlers._redirect_to_relay_verification(41)

    assert ledger.create_calls == 0
    assert controller.command_calls == []
    assert controller.text_calls == []
    assert len(bot.messages) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "text"),
    [
        ("new_cmd", "/new"),
        ("codex_cmd", "/codex inspect this"),
        ("claude_cmd", "/claude implement this"),
        ("auto_cmd", "/auto implement this"),
        ("conversation_text", "please implement this"),
        ("task", "/task demo do not create old task"),
        ("stop_cmd", "/stop"),
    ],
)
async def test_new_telegram_traffic_redirects_without_legacy_state(
    method_name: str, text: str
) -> None:
    ledger = _Ledger()
    controller = _Controller()
    bot = _Bot()
    handlers = _handlers(ledger, controller, bot)

    await getattr(handlers, method_name)(_update(text), None)

    assert ledger.create_calls == 0
    assert controller.command_calls == []
    assert controller.text_calls == []
    assert "Telegram 现仅保留历史会话兼容" in str(bot.messages[-1]["text"])
    assert "兼容状态：状态已陈旧（Telegram 跳转入口）" in str(bot.messages[-1]["text"])
    assert "下一步：打开 Native 开始直接会话，或打开 Relay 创建任务。" in str(
        bot.messages[-1]["text"]
    )
    assert _button_actions(bot) == [
        ("https://native.yjxjj.xyz/native", None),
        ("https://native.yjxjj.xyz/native/workflows/relay", None),
    ]


@pytest.mark.asyncio
async def test_persisted_legacy_conversation_keeps_plain_text_flow() -> None:
    ledger = _Ledger(SimpleNamespace(id=3, legacy_compatible=True))
    controller = _Controller()
    bot = _Bot()
    handlers = _handlers(ledger, controller, bot)

    await handlers.conversation_text(_update("continue historical work"), None)

    assert controller.text_calls == ["continue historical work"]
    assert bot.messages[-1]["text"] == "legacy text"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "text", "expected_url"),
    [
        ("native_cmd", "/native", "https://native.yjxjj.xyz/native"),
        ("relay_cmd", "/relay", "https://native.yjxjj.xyz/native/workflows/relay"),
    ],
)
async def test_surface_commands_are_url_jumps_without_legacy_mutation(
    method_name: str, text: str, expected_url: str
) -> None:
    ledger = _Ledger()
    controller = _Controller()
    bot = _Bot()
    handlers = _handlers(ledger, controller, bot)

    await getattr(handlers, method_name)(_update(text), None)

    assert ledger.create_calls == 0
    assert controller.command_calls == []
    assert "兼容状态：状态已陈旧（Telegram 跳转入口）" in str(
        bot.messages[-1]["text"]
    )
    assert f"下一步：打开 {method_name.removesuffix('_cmd').title()}。" in str(
        bot.messages[-1]["text"]
    )
    assert _button_actions(bot) == [(expected_url, None)]


@pytest.mark.asyncio
async def test_legacy_verify_hands_off_to_relay_without_claiming_test_execution() -> None:
    ledger = _Ledger(SimpleNamespace(id=3, legacy_compatible=True))
    controller = _Controller()
    bot = _Bot()
    handlers = _handlers(ledger, controller, bot)

    await handlers.verify_cmd(_update("/verify"), None)

    assert controller.command_calls == []
    assert "不会执行声明的测试" in str(bot.messages[-1]["text"])
    assert "兼容状态：状态已陈旧（Telegram 历史兼容）" in str(
        bot.messages[-1]["text"]
    )
    assert "下一步：在 Relay 打开对应任务，并执行绑定 implementation run 的目标验收。" in str(
        bot.messages[-1]["text"]
    )
    assert _button_actions(bot) == [
        ("https://native.yjxjj.xyz/native/workflows/relay", None)
    ]


def test_button_model_serializes_exactly_one_action() -> None:
    assert _telegram_button({"text": "open", "url": "https://example.test"}) == {
        "text": "open",
        "url": "https://example.test",
    }
    assert _telegram_button({"text": "run", "callback_data": "conv:1:continue"}) == {
        "text": "run",
        "callback_data": "conv:1:continue",
    }
    with pytest.raises(ValueError, match="exactly one"):
        _telegram_button(
            {
                "text": "ambiguous",
                "url": "https://example.test",
                "callback_data": "conv:1:continue",
            }
        )
    with pytest.raises(ValueError, match="exactly one"):
        _telegram_button({"text": "inert"})


def test_archived_legacy_history_is_allowed_only_when_no_active_new_conversation() -> None:
    archived = SimpleNamespace(id=3, legacy_compatible=True)
    controller = _Controller()
    bot = _Bot()

    historical = _handlers(_Ledger(archived=tuple([archived])), controller, bot)
    assert historical._has_legacy_compatible_conversation(41, include_archived=True)

    active_new = _handlers(
        _Ledger(SimpleNamespace(id=4, legacy_compatible=False), tuple([archived])),
        controller,
        bot,
    )
    assert not active_new._has_legacy_compatible_conversation(41, include_archived=True)
