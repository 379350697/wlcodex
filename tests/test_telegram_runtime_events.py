from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
import ast

import pytest


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

    error.TelegramError = TelegramError
    error.NetworkError = NetworkError
    error.TimedOut = TimedOut

    ext = types.ModuleType("telegram.ext")
    ext.Application = SimpleNamespace(builder=lambda: None)
    ext.CallbackQueryHandler = object
    ext.CommandHandler = object
    ext.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)
    ext.MessageHandler = object
    ext.filters = SimpleNamespace(TEXT=object(), COMMAND=object())

    sys.modules.setdefault("telegram", telegram)
    sys.modules.setdefault("telegram.error", error)
    sys.modules.setdefault("telegram.ext", ext)


def _handlers(tmp_path: Path, *, controller: object | None = None, bot: object | None = None):
    _install_telegram_stubs()
    from wlcodex.db import Ledger
    from wlcodex.runtime_event_store import RuntimeEventStore
    from wlcodex.telegram_app import WlCodexHandlers

    ledger = Ledger.open(tmp_path / "db.sqlite3")
    ledger.migrate()
    store = RuntimeEventStore(ledger._conn)
    config = SimpleNamespace(
        telegram=SimpleNamespace(allowed_user_ids=frozenset({456})),
        interaction=SimpleNamespace(
            profile="natural",
            edit_min_interval_seconds=1.0,
        ),
    )
    controller = controller or SimpleNamespace()
    approval = SimpleNamespace()
    bot = bot or SimpleNamespace()
    return WlCodexHandlers(config, controller, ledger, approval, bot, store), ledger, store


def test_handlers_do_not_bypass_eventized_delivery() -> None:
    source = Path("wlcodex/telegram_app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "WlCodexHandlers":
            continue
        for method in node.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(method):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                if isinstance(func, ast.Attribute) and func.attr == "reply_text":
                    violations.append(f"{method.name}:{call.lineno}:reply_text")
                if (
                    method.name != "edit_telegram"
                    and isinstance(func, ast.Attribute)
                    and func.attr == "edit_message_text"
                ):
                    violations.append(f"{method.name}:{call.lineno}:edit_message_text")

    assert violations == []


def test_telegram_delivery_uses_active_runtime_correlation(tmp_path: Path) -> None:
    from wlcodex.runtime_events import (
        AggregateType,
        EventSource,
        EventType,
        RuntimeEvent,
        Visibility,
        now_iso,
    )

    handlers, ledger, store = _handlers(tmp_path)
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="runtime",
        mode="chief_engineer",
        workspace_alias="demo",
    )
    run_event = store.append(RuntimeEvent(
        schema_version=1,
        event_type=EventType.RUN_COMPLETED,
        aggregate_type=AggregateType.ORCHESTRATION_RUN,
        aggregate_id="7",
        correlation_id="corr-user-request",
        source=EventSource.ORCHESTRATOR,
        actor="orchestrator",
        visibility=Visibility.USER,
        payload={"verify_round": 1},
        occurred_at=now_iso(),
        conversation_id=conversation.id,
        orchestration_run_id=7,
    ))

    handlers._append_telegram_delivery_event(
        EventType.TELEGRAM_MESSAGE_SENT,
        chat_id=123,
        text="done",
        message_id=99,
    )

    events = store.list_by_correlation("corr-user-request")
    assert [event.event_type for event in events] == [
        EventType.RUN_COMPLETED,
        EventType.TELEGRAM_MESSAGE_SENT,
    ]
    assert events[-1].causation_id == run_event.id


@pytest.mark.asyncio
async def test_callback_router_appends_callback_received_event(tmp_path: Path) -> None:
    from wlcodex.runtime_events import EventType

    handlers, ledger, store = _handlers(tmp_path)
    conversation = ledger.create_conversation(
        chat_id=123,
        user_id=456,
        title="runtime",
        mode="chief_engineer",
        workspace_alias="demo",
    )

    class Query:
        data = "unknown:value"
        id = "cb-1"
        message = SimpleNamespace(message_id=44, text="button")

        async def answer(self, _text: str) -> None:
            return None

    update = SimpleNamespace(
        update_id=1,
        callback_query=Query(),
        effective_user=SimpleNamespace(id=456),
        effective_chat=SimpleNamespace(id=123, type="private"),
        effective_message=SimpleNamespace(text=None),
    )

    await handlers.callback_router(update, SimpleNamespace())

    events = store.list_by_conversation(conversation.id)
    assert any(
        event.event_type == EventType.TELEGRAM_CALLBACK_RECEIVED
        and event.payload["callback_data"] == "unknown:value"
        for event in events
    )


@pytest.mark.asyncio
async def test_health_delivery_timeout_is_eventized(tmp_path: Path) -> None:
    _install_telegram_stubs()
    from telegram.error import TimedOut
    from wlcodex.runtime_events import EventType

    class Controller:
        async def handle(self, _text: str, _ctx: dict[str, object]) -> object:
            return SimpleNamespace(text="health ok", buttons=[])

    class Bot:
        async def send_message(self, **_kwargs: object) -> object:
            raise TimedOut("network timeout")

    class Message:
        text = "/health"

        async def reply_text(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("health must use send_telegram")

    handlers, _ledger, store = _handlers(
        tmp_path,
        controller=Controller(),
        bot=Bot(),
    )
    update = SimpleNamespace(
        update_id=2,
        effective_user=SimpleNamespace(id=456),
        effective_chat=SimpleNamespace(id=123, type="private"),
        effective_message=Message(),
    )

    await handlers.health(update, SimpleNamespace())

    row = store._conn.execute(
        "SELECT event_type, payload_json FROM runtime_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row["event_type"] == EventType.TELEGRAM_MESSAGE_FAILED
    assert "health ok" in row["payload_json"]
