from __future__ import annotations

import importlib.util
import sys
import types
from types import SimpleNamespace


def _install_telegram_stubs() -> None:
    telegram = types.ModuleType("telegram")
    telegram.Update = object

    class InlineKeyboardButton:
        def __init__(self, text: str, callback_data: str = "") -> None:
            self.text = text
            self.callback_data = callback_data

    class InlineKeyboardMarkup:
        def __init__(self, keyboard: object) -> None:
            self.keyboard = keyboard

    telegram.InlineKeyboardButton = InlineKeyboardButton
    telegram.InlineKeyboardMarkup = InlineKeyboardMarkup

    error = types.ModuleType("telegram.error")

    class TelegramError(Exception):
        pass

    class NetworkError(TelegramError):
        pass

    class TimedOut(NetworkError):
        pass

    class Forbidden(TelegramError):
        pass

    error.TelegramError = TelegramError
    error.NetworkError = NetworkError
    error.TimedOut = TimedOut
    error.Forbidden = Forbidden

    constants = types.ModuleType("telegram.constants")
    constants.ChatAction = SimpleNamespace(TYPING="typing")

    class _Filter:
        def __and__(self, other: object) -> "_Filter":
            return self

        def __invert__(self) -> "_Filter":
            return self

    class _CommandHandler:
        def __init__(self, commands: str | list[str], callback: object) -> None:
            if isinstance(commands, str):
                self.commands = frozenset({commands})
            else:
                self.commands = frozenset(commands)
            self.callback = callback

    class _CallbackQueryHandler:
        def __init__(self, callback: object) -> None:
            self.callback = callback

    class _MessageHandler:
        def __init__(self, filter_: object, callback: object) -> None:
            self.filter = filter_
            self.callback = callback

    class _Application:
        def __init__(self, max_concurrent_updates: int = 1) -> None:
            self.handlers: dict[int, list[object]] = {}
            self.bot = SimpleNamespace()
            self.update_processor = SimpleNamespace(
                max_concurrent_updates=max_concurrent_updates
            )

        @classmethod
        def builder(cls) -> "_ApplicationBuilder":
            return _ApplicationBuilder()

        def add_handler(self, handler: object, group: int = 0) -> None:
            self.handlers.setdefault(group, []).append(handler)

    class _ApplicationBuilder:
        def __init__(self) -> None:
            self._max_concurrent_updates = 1

        def token(self, token: str) -> "_ApplicationBuilder":
            return self

        def concurrent_updates(self, count: int) -> "_ApplicationBuilder":
            self._max_concurrent_updates = count
            return self

        def build(self) -> _Application:
            return _Application(self._max_concurrent_updates)

    ext = types.ModuleType("telegram.ext")
    ext.Application = _Application
    ext.CallbackQueryHandler = _CallbackQueryHandler
    ext.CommandHandler = _CommandHandler
    ext.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)
    ext.MessageHandler = _MessageHandler
    ext.filters = SimpleNamespace(TEXT=_Filter(), COMMAND=_Filter())

    sys.modules.setdefault("telegram", telegram)
    sys.modules.setdefault("telegram.error", error)
    sys.modules.setdefault("telegram.constants", constants)
    sys.modules.setdefault("telegram.ext", ext)


if importlib.util.find_spec("telegram") is None:
    _install_telegram_stubs()
