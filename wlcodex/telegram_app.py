from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

from telegram import Update
from telegram.error import NetworkError, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from wlcodex.config import AppConfig
from wlcodex.controller import CommandController
from wlcodex.db import Ledger
from wlcodex.interaction.profiles import profile_from_name
from wlcodex.interaction.renderer import InteractionRenderer
from wlcodex.interaction.transport import TelegramTransport
from wlcodex.streaming import StreamingRenderer

logger = logging.getLogger(__name__)


def _is_telegram_network_error(exc: Exception) -> bool:
    """Return True if the exception is a transient Telegram network error."""
    if isinstance(exc, (TimedOut, NetworkError)):
        return True
    msg = str(exc).lower()
    return any(
        m in msg
        for m in ("timed out", "connect timeout", "connectionerror", "networkerror")
    )


def _is_message_not_modified_error(exc: Exception) -> bool:
    return "message is not modified" in str(exc).lower()


# Sentinel returned when a Telegram send fails due to a network error.
SEND_FAILED = -1


def is_authorized(
    user_id: int | None, chat_type: str, allowed_user_ids: frozenset[int]
) -> bool:
    if user_id is None:
        return False
    if chat_type != "private":
        return False
    return user_id in allowed_user_ids


def ensure_authorized(
    update: Update, allowed_user_ids: frozenset[int]
) -> bool:
    effective = update.effective_user
    effective_chat = update.effective_chat
    user_id = effective.id if effective else None
    chat_type = effective_chat.type if effective_chat else "unknown"
    allowed = is_authorized(user_id, chat_type, allowed_user_ids)
    if not allowed:
        logger.info("Rejected update: user=%s chat=%s", user_id, chat_type)
    return allowed


class WlCodexHandlers:
    """Container for all authenticated Telegram command handlers."""

    def __init__(
        self,
        config: AppConfig,
        controller: CommandController,
        ledger: Ledger,
        approval_service: object,
        bot: object,
        runtime_event_store: object | None = None,
        outbox: object | None = None,
        terminal_manager: object | None = None,
    ) -> None:
        self._config = config
        self._controller = controller
        self._ledger = ledger
        self._approval = approval_service
        self._bot = bot
        self._runtime_store = runtime_event_store
        self._outbox = outbox
        self._terminal_manager = terminal_manager
        # BLOCKER B: pending historical continuation per conversation_id
        self._pending_continuation: dict[int, dict] = {}

    # --- Auth guard ---

    def _guard(self, update: Update) -> bool:
        ok = ensure_authorized(update, self._config.telegram.allowed_user_ids)
        if ok and update.effective_user:
            update_text = (
                update.effective_message.text
                if update.effective_message and update.effective_message.text
                else ""
            )
            self._ledger.record_telegram_update(
                update_id=update.update_id,
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id if update.effective_chat else 0,
                update_type=update_text or "callback",
                allowed=True,
            )
            if update_text.startswith("/"):
                self._append_telegram_command_received_event(update, update_text)
        elif not ok and update.effective_user:
            self._ledger.record_telegram_update(
                update_id=update.update_id,
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id if update.effective_chat else 0,
                update_type="rejected",
                allowed=False,
            )
        return ok

    def _append_telegram_command_received_event(
        self, update: Update, text: str
    ) -> None:
        if self._runtime_store is None:
            return
        try:
            from wlcodex.runtime_events import (
                AggregateType,
                EventSource,
                EventType,
                RuntimeEvent,
                Visibility,
                now_iso,
            )

            chat = update.effective_chat
            user = update.effective_user
            chat_id = chat.id if chat is not None else 0
            active = self._ledger.get_active_conversation(chat_id)
            conversation_id = active.id if active is not None else None
            aggregate_type = (
                AggregateType.CONVERSATION
                if active is not None
                else AggregateType.TELEGRAM_MESSAGE
            )
            aggregate_id = str(active.id if active is not None else update.update_id)
            self._runtime_store.append(RuntimeEvent(
                schema_version=1,
                event_type=EventType.USER_MESSAGE_RECEIVED,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                correlation_id=f"user-command-{update.update_id}",
                source=EventSource.TELEGRAM,
                actor="user",
                visibility=Visibility.USER,
                payload={
                    "chat_id": chat_id,
                    "user_id": user.id if user is not None else None,
                    "telegram_update_id": update.update_id,
                    "command": text.split(maxsplit=1)[0][:100],
                    "text_preview": text[:500],
                },
                occurred_at=now_iso(),
                conversation_id=conversation_id,
            ))
        except Exception:
            logger.debug("Failed to append Telegram command event", exc_info=True)

    async def _store_status_message(
        self, task_id: int, chat_id: int, message_id: int
    ) -> None:
        self._ledger.set_status_message(task_id, chat_id, message_id)

    async def _reply_with_buttons(
        self,
        update: Update,
        text: str,
        buttons: list[list[dict[str, str]]] | None = None,
    ) -> object:
        """Send a reply through the eventized Telegram delivery bridge."""
        return await self._send_response(update, text, buttons)

    async def _send_response(
        self,
        update: Update,
        text: str,
        buttons: list[list[dict[str, str]]] | None = None,
    ) -> object:
        message_id = await self.send_telegram(update.effective_chat.id, text, buttons)
        return SimpleNamespace(message_id=message_id)

    # --- Callback answer/edit safety wrappers ---

    async def _safe_callback_answer(
        self, query: object, text: str, *, correlation_id: str = ""
    ) -> None:
        """Answer a callback query — failure is recorded, never raised.

        Callback answer failures must NEVER affect approval resolution or
        business logic. They are recorded as telegram.callback.answer.failed.
        """
        if self._outbox is not None:
            async def _answer(text: str) -> None:
                await query.answer(text=text)
            self._outbox.enqueue_answer_callback(
                text,
                answer_fn=_answer,
                correlation_id=correlation_id or "outbox-callback-answer",
            )
            return
        # Direct path: answer and record failure if it happens.
        try:
            await query.answer(text=text)
        except Exception as exc:
            logger.warning("Callback answer failed: %s", exc)
            from wlcodex.runtime_events import EventType
            self._append_telegram_delivery_event(
                EventType.TELEGRAM_CALLBACK_ANSWER_FAILED,
                chat_id=0,
                text=text,
                error=str(exc),
            )

    async def _safe_callback_edit(
        self,
        update: Update,
        query: object,
        text: str,
        buttons: list[list[dict[str, str]]] | None = None,
    ) -> None:
        """Edit the callback message — failure is recorded, never raised.

        Callback edit failures must NEVER affect approval resolution.
        Failed edits append telegram.callback.edit.failed and do not
        prevent the business logic from completing.
        """
        try:
            await self._edit_callback_message(update, query, text, buttons)
        except Exception as exc:
            logger.warning("Callback edit failed: %s", exc)
            from wlcodex.runtime_events import EventType
            self._append_telegram_delivery_event(
                EventType.TELEGRAM_CALLBACK_EDIT_FAILED,
                chat_id=update.effective_chat.id if update.effective_chat else 0,
                text=text,
                error=str(exc),
            )

    async def _edit_callback_message(
        self,
        update: Update,
        query: object,
        text: str,
        buttons: list[list[dict[str, str]]] | None = None,
    ) -> None:
        chat = update.effective_chat
        message = getattr(query, "message", None)
        message_chat = getattr(message, "chat", None)
        chat_id = (
            chat.id
            if chat is not None
            else getattr(message, "chat_id", getattr(message_chat, "id", 0))
        )
        message_id = getattr(message, "message_id", None)
        if chat_id and message_id is not None:
            await self.edit_telegram(chat_id, int(message_id), text, buttons=buttons)
            return
        if chat_id:
            await self.send_telegram(chat_id, text, buttons)

    # --- Typing and streaming ---

    async def _start_typing(self, chat_id: int) -> object:
        """Start a typing indicator task for the given chat."""
        import asyncio
        from telegram.constants import ChatAction

        async def _keep_typing() -> None:
            try:
                while True:
                    await self._bot.send_chat_action(
                        chat_id=chat_id, action=ChatAction.TYPING
                    )
                    await asyncio.sleep(4.0)
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        task = asyncio.create_task(_keep_typing())
        return task

    def create_interaction_renderer(self) -> InteractionRenderer:
        interaction = getattr(self._config, "interaction", None)
        profile_name = getattr(interaction, "profile", "legacy")
        min_interval = float(
            getattr(interaction, "edit_min_interval_seconds", 1.0)
        )
        transport = TelegramTransport(
            self.send_telegram,
            self.edit_telegram,
            self._start_typing,
        )
        return InteractionRenderer(
            transport=transport,
            profile=profile_from_name(profile_name),
            min_interval_seconds=min_interval,
        )

    def create_streaming_renderer(self, chat_id: int) -> StreamingRenderer:
        """Create a StreamingRenderer bound to this handler's send/edit callbacks."""
        return StreamingRenderer(
            send_fn=self.send_telegram,
            edit_fn=self.edit_telegram,
            min_interval_seconds=1.0,
        )

    # --- Telegram send/edit callbacks for event bridge ---

    async def send_telegram(
        self, chat_id: int, text: str, buttons: list[list[dict[str, str]]] | None = None
    ) -> int:
        """Send a message via the bot, routed through the outbox when available.

        Returns message_id, or SEND_FAILED (-1) on transient network errors.
        When the outbox is active, delivery events are recorded exclusively by
        the outbox; the raw send fn raises on failure so the outbox can retry.
        """
        if self._outbox is not None:
            self._outbox.enqueue_send(
                chat_id, text, buttons,
                send_fn=self._raw_send_message,
                edit_fn=self._raw_edit_message,
                correlation_id="outbox-send",
            )
            return SEND_FAILED

        # Direct path (no outbox): call raw API and record delivery events.
        try:
            msg_id = await self._raw_send_message(chat_id, text, buttons)
            self._append_telegram_delivery_event(
                "telegram.message.sent",
                chat_id=chat_id, text=text, message_id=msg_id,
            )
            return msg_id
        except Exception as exc:
            if _is_telegram_network_error(exc):
                self._append_telegram_delivery_event(
                    "telegram.message.failed",
                    chat_id=chat_id, text=text, error=str(exc),
                )
                return SEND_FAILED
            self._append_telegram_delivery_event(
                "telegram.message.failed",
                chat_id=chat_id, text=text, error=str(exc),
            )
            raise

    async def _raw_send_message(
        self, chat_id: int, text: str, buttons: list[list[dict[str, str]]] | None = None
    ) -> int:
        """Direct Bot API send — raises on any error (no event recording).

        Used by the outbox as its send_fn. The outbox catches exceptions,
        retries, and records all delivery events. This function is a pure
        Telegram API call.
        """
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        reply_markup = None
        if buttons:
            keyboard = [
                [InlineKeyboardButton(b["text"], callback_data=b["callback_data"])
                 for b in row]
                for row in buttons
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

        msg = await self._bot.send_message(
            chat_id=chat_id, text=text, reply_markup=reply_markup
        )
        return msg.message_id

    async def edit_telegram(
        self, chat_id: int, message_id: int, text: str,
        buttons: list[list[dict[str, str]]] | None = None,
    ) -> None:
        """Edit an existing message, routed through the outbox when available.

        When the outbox is active, delivery events are recorded exclusively by
        the outbox; the raw edit fn raises on failure so the outbox can retry.
        """
        if self._outbox is not None:
            self._outbox.enqueue_edit(
                chat_id, message_id, text, buttons,
                edit_fn=self._raw_edit_message,
                correlation_id="outbox-edit",
            )
            return

        # Direct path (no outbox): call raw API and record delivery events.
        try:
            await self._raw_edit_message(chat_id, message_id, text, buttons)
            self._append_telegram_delivery_event(
                "telegram.message.edited",
                chat_id=chat_id, text=text, message_id=message_id,
            )
        except Exception as exc:
            if _is_message_not_modified_error(exc):
                self._append_telegram_delivery_event(
                    "telegram.edit.skipped_no_change",
                    chat_id=chat_id, text=text, message_id=message_id,
                )
                return
            if _is_telegram_network_error(exc):
                self._append_telegram_delivery_event(
                    "telegram.message.failed",
                    chat_id=chat_id, text=text, message_id=message_id,
                    error=str(exc),
                )
                return
            # Non-retryable error → try fallback send
            logger.debug("Failed to edit message %d, sending new one", message_id)
            await self._fallback_send_on_edit_failure(
                chat_id, message_id, text, buttons,
            )

    async def _raw_edit_message(
        self, chat_id: int, message_id: int, text: str,
        buttons: list[list[dict[str, str]]] | None = None,
    ) -> None:
        """Direct Bot API edit — raises on any error (no event recording).

        Used by the outbox as its edit_fn. The outbox catches exceptions,
        retries, and records all delivery events.  Note: "message is not
        modified" is also raised so the outbox can handle it.
        """
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        reply_markup = None
        if buttons:
            keyboard = [
                [InlineKeyboardButton(b["text"], callback_data=b["callback_data"])
                 for b in row]
                for row in buttons
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

        await self._bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text,
            reply_markup=reply_markup,
        )

    async def _fallback_send_on_edit_failure(
        self, chat_id: int, message_id: int, text: str,
        buttons: list[list[dict[str, str]]] | None = None,
    ) -> None:
        """Send a new message when edit fails with non-retryable error."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        reply_markup = None
        if buttons:
            keyboard = [
                [InlineKeyboardButton(b["text"], callback_data=b["callback_data"])
                 for b in row]
                for row in buttons
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            new_msg = await self._bot.send_message(
                chat_id=chat_id, text=text, reply_markup=reply_markup,
            )
            self._append_telegram_delivery_event(
                "telegram.message.sent",
                chat_id=chat_id, text=text, message_id=new_msg.message_id,
                payload_extra={"fallback_for_message_id": message_id},
            )
        except Exception as send_exc:
            logger.warning(
                "Telegram edit fallback send failed: chat_id=%s exc=%s",
                chat_id, send_exc,
            )
            self._append_telegram_delivery_event(
                "telegram.message.failed",
                chat_id=chat_id, text=text, message_id=message_id,
                error=str(send_exc),
            )
            return
        for task in self._ledger.list_tasks(limit=50, include_archived=True):
            if task.telegram_status_message_id == message_id:
                self._ledger.set_status_message(task.id, chat_id, new_msg.message_id)

    def _append_telegram_delivery_event(
        self,
        event_type: str,
        *,
        chat_id: int,
        text: str,
        message_id: int | None = None,
        error: str = "",
        payload_extra: dict[str, object] | None = None,
    ) -> None:
        if self._runtime_store is None:
            return
        try:
            from wlcodex.runtime_events import (
                AggregateType,
                EventSource,
                EventType,
                RuntimeEvent,
                Visibility,
                now_iso,
            )

            active = self._ledger.get_active_conversation(chat_id)
            conversation_id = active.id if active is not None else None
            aggregate_id = str(message_id or f"{chat_id}-{now_iso()}")
            correlation_id, causation_id = self._active_runtime_context(
                conversation_id,
                fallback=f"telegram-{aggregate_id}",
            )
            payload: dict[str, object] = {
                "chat_id": chat_id,
                "message_id": message_id,
                "text_preview": text[:500],
                "text_length": len(text),
            }
            if error:
                payload["error"] = error[:1000]
            if payload_extra:
                payload.update(payload_extra)
            self._runtime_store.append(RuntimeEvent(
                schema_version=1,
                event_type=event_type,
                aggregate_type=AggregateType.TELEGRAM_MESSAGE,
                aggregate_id=aggregate_id,
                correlation_id=correlation_id,
                source=EventSource.TELEGRAM,
                actor="telegram_bot",
                visibility=Visibility.OPERATOR,
                payload=payload,
                occurred_at=now_iso(),
                conversation_id=conversation_id,
                causation_id=causation_id,
            ))
        except Exception:
            logger.debug("Failed to append Telegram delivery event", exc_info=True)

    def _active_runtime_context(
        self,
        conversation_id: int | None,
        *,
        fallback: str,
    ) -> tuple[str, int | None]:
        if self._runtime_store is None or conversation_id is None:
            return fallback, None
        row = self._runtime_store._conn.execute(
            """
            SELECT id, correlation_id FROM runtime_events
            WHERE conversation_id = ?
              AND correlation_id NOT LIKE 'telegram-%'
            ORDER BY id DESC
            LIMIT 1
            """,
            (conversation_id,),
        ).fetchone()
        if row is None:
            return fallback, None
        return str(row["correlation_id"]), int(row["id"])

    def _append_telegram_callback_event(self, update: Update, data: str) -> None:
        if self._runtime_store is None:
            return
        try:
            from wlcodex.runtime_events import (
                AggregateType,
                EventSource,
                EventType,
                RuntimeEvent,
                Visibility,
                now_iso,
            )

            chat = update.effective_chat
            user = update.effective_user
            query = update.callback_query
            chat_id = chat.id if chat is not None else 0
            active = self._ledger.get_active_conversation(chat_id)
            conversation_id = active.id if active is not None else None
            callback_id = str(getattr(query, "id", "") or f"{chat_id}-{now_iso()}")
            message = getattr(query, "message", None)
            message_id = getattr(message, "message_id", None)
            correlation_id, causation_id = self._active_runtime_context(
                conversation_id,
                fallback=f"telegram-callback-{callback_id}",
            )
            self._runtime_store.append(RuntimeEvent(
                schema_version=1,
                event_type=EventType.TELEGRAM_CALLBACK_RECEIVED,
                aggregate_type=AggregateType.TELEGRAM_MESSAGE,
                aggregate_id=callback_id,
                correlation_id=correlation_id,
                source=EventSource.TELEGRAM,
                actor="user",
                visibility=Visibility.OPERATOR,
                payload={
                    "chat_id": chat_id,
                    "user_id": user.id if user is not None else None,
                    "callback_id": callback_id,
                    "callback_data": data[:500],
                    "message_id": message_id,
                },
                occurred_at=now_iso(),
                conversation_id=conversation_id,
                causation_id=causation_id,
            ))
        except Exception:
            logger.debug("Failed to append Telegram callback event", exc_info=True)

    # --- Handlers ---

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        profile_name = getattr(
            getattr(self._config, "interaction", None), "profile", "legacy"
        )
        from wlcodex.status import render_conversation_help
        await self.send_telegram(
            update.effective_chat.id,
            render_conversation_help(profile=profile_name),
        )

    async def help_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        profile_name = getattr(
            getattr(self._config, "interaction", None), "profile", "legacy"
        )
        from wlcodex.status import render_conversation_help
        await self.send_telegram(
            update.effective_chat.id,
            render_conversation_help(profile=profile_name),
        )

    async def settings_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        await self.send_telegram(
            update.effective_chat.id,
            "⚙️ 设置\n\n"
            "默认流程：Codex → Claude → Codex\n"
            "当前视图：驾驶舱\n\n"
            "你可以调整：",
            buttons=[
                [{"text": "默认流程（Codex → Claude → Codex）",
                  "callback_data": "settings:exec_mode:orchestrated"}],
                [{"text": "只问 Codex",
                  "callback_data": "settings:exec_mode:codex_direct"}],
                [{"text": "只叫 Claude",
                  "callback_data": "settings:exec_mode:claude_direct"}],
                [{"text": "模型", "callback_data": "settings:model"}],
                [{"text": "Claude 权限", "callback_data": "settings:claude_permission:normal"}],
                [{"text": "工作区", "callback_data": "settings:workspace"}],
            ],
        )

    async def task(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        text = update.effective_message.text
        ctx = {"chat_id": update.effective_chat.id, "user_id": update.effective_user.id}
        response = await self._controller.handle(text, ctx)

        sent = await self._reply_with_buttons(
            update, response.text, response.buttons
        )
        # If this was a StartTaskCommand, store the message for status editing
        from wlcodex.router import parse_command, StartTaskCommand
        try:
            cmd = parse_command(text)
            if isinstance(cmd, StartTaskCommand):
                if "任务 #" in response.text or "Task #" in response.text:
                    import re
                    m = re.search(r"(?:任务|Task) #(\d+)", response.text)
                    if m and sent.message_id != SEND_FAILED:
                        task_id = int(m.group(1))
                        await self._store_status_message(
                            task_id, update.effective_chat.id, sent.message_id
                        )
        except Exception:
            pass  # Best effort

    async def tasks_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await self.send_telegram(update.effective_chat.id, response.text)

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle("/status", _ctx(update))
        await self.send_telegram(update.effective_chat.id, response.text)

    async def continue_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        sent = await self._send_response(update, response.text)
        # Track status message for continue too
        await self._track_status_from_response(response.text, update.effective_chat.id, sent.message_id)

    async def steer(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await self.send_telegram(update.effective_chat.id, response.text)

    async def tail(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await self.send_telegram(update.effective_chat.id, response.text)

    async def events(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await self.send_telegram(update.effective_chat.id, response.text)

    async def diff(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await self.send_telegram(update.effective_chat.id, response.text)

    async def files(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await self.send_telegram(update.effective_chat.id, response.text)

    async def pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await self.send_telegram(update.effective_chat.id, response.text)

    async def abort(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await self.send_telegram(update.effective_chat.id, response.text)

    async def archive(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await self.send_telegram(update.effective_chat.id, response.text)

    async def fork(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        sent = await self._send_response(update, response.text)
        await self._track_status_from_response(response.text, update.effective_chat.id, sent.message_id)

    async def codex_sessions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        chat_id = update.effective_chat.id

        if self._ledger is not None:
            from wlcodex.workbench.sessions import AgentSessionLibrary
            from wlcodex.workbench.rendering import render_session_library

            active = self._ledger.get_active_conversation(chat_id)
            if active is not None:
                library = AgentSessionLibrary(self._ledger)
                sessions = library.list_for_workbench(active.id)
                text = render_session_library(sessions)
                buttons = self._render_session_picker_buttons(active.id, sessions)
                await self.send_telegram(chat_id, text, buttons=buttons)
                return

        await self.send_telegram(
            chat_id,
            "当前还没有工作台。发送 /new 开始一个新的工作台。",
        )

    async def health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle("/health", _ctx(update))
        await self.send_telegram(update.effective_chat.id, response.text)

    # --- Dual-surface mode command handlers ---

    async def mode_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        text = update.effective_message.text
        chat_id = update.effective_chat.id

        from wlcodex.router import parse_command, ModeSwitchCommand

        try:
            command = parse_command(text)
        except Exception:
            await self.send_telegram(chat_id, "未知命令。发送 /help 查看可用命令。")
            return

        if isinstance(command, ModeSwitchCommand):
            if command.mode == "":
                # /mode - show current mode
                active = self._ledger.get_active_conversation(chat_id)
                current_mode = "product"
                if self._runtime_store is not None and active is not None:
                    try:
                        row = self._runtime_store._conn.execute(
                            "SELECT * FROM runtime_events WHERE conversation_id = ? "
                            "AND event_type = 'conversation.mode.switched' "
                            "ORDER BY id DESC LIMIT 1",
                            (active.id,),
                        ).fetchone()
                        if row is not None:
                            import json as _json
                            raw = row["payload"]
                            payload = _json.loads(raw) if isinstance(raw, str) else raw
                            current_mode = payload.get("to_mode", "product")
                    except Exception:
                        pass
                view_name = "驾驶舱" if current_mode == "product" else "现场"
                await self.send_telegram(chat_id, f"当前视图：{view_name}。使用 /product 或 /terminal 切换。")
                return
            else:
                await self._apply_mode_switch(update, command)
                return

        await self.send_telegram(chat_id, "未知视图命令。使用 /product 或 /terminal 切换。")

    async def product_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        from wlcodex.router import ModeSwitchCommand
        command = ModeSwitchCommand(mode="product")
        await self._apply_mode_switch(update, command)

    async def terminal_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        text = update.effective_message.text
        chat_id = update.effective_chat.id

        from wlcodex.router import parse_command, ModeSwitchCommand, TerminalSubCommand

        try:
            command = parse_command(text)
        except Exception as e:
            await self.send_telegram(chat_id, str(e))
            return

        # Check terminal.enabled before allowing mode switch or subcommands
        terminal_config = getattr(self._config, "terminal", None)
        terminal_enabled = getattr(terminal_config, "enabled", False) if terminal_config is not None else False

        if isinstance(command, ModeSwitchCommand):
            # /terminal product is the escape hatch back to product mode
            # — always allowed even when terminal is disabled.
            if command.mode != "product" and not terminal_enabled:
                await self.send_telegram(
                    chat_id,
                    "现场接管当前不可用。驾驶舱仍可正常工作。"
                )
                return
            await self._apply_mode_switch(update, command)
            return

        if isinstance(command, TerminalSubCommand):
            if command.subcommand == "tail":
                await self.send_telegram(
                    chat_id,
                    "现场 tail 功能将在现场会话实现后可用。"
                )
            elif command.subcommand == "pause":
                await self.send_telegram(
                    chat_id,
                    "现场推送已暂停。使用 /terminal tail 恢复查看。"
                )
            elif command.subcommand == "detach":
                await self.send_telegram(
                    chat_id,
                    "已离开现场，现场会话仍在运行。使用 /terminal 重新接入。"
                )
            return

        await self.send_telegram(
            chat_id,
            "未知现场命令。用法：/terminal [claude|codex|agent claude|agent codex|tail|pause|detach|product]"
        )

    def _find_external_session_id(self, conversation_id: int, agent: str) -> str | None:
        """Look up an existing external session id from agent_runs.

        Returns the most recent non-null external_session_id for *agent*
        within *conversation_id*, or None when no run exists / no id is set.

        Uses ``list_recent_agent_runs`` (newest-first) so that conversations
        with more than 50 agent runs still find the latest session id.
        """
        if conversation_id is None:
            return None
        try:
            runs = self._ledger.list_recent_agent_runs(conversation_id, limit=50)
        except Exception:
            logger.debug("Failed to list agent runs for session lookup", exc_info=True)
            return None
        for run in runs:
            if run.agent == agent and run.external_session_id:
                return run.external_session_id
        return None

    def _get_active_surface_mode(self, chat_id: int) -> str:
        """Query the current surface mode for a chat from runtime events.

        Returns 'product' if unset or if the store is unavailable.
        Terminal sessions that became orphaned still report 'terminal'.
        """
        if self._runtime_store is None:
            return "product"
        try:
            active = self._ledger.get_active_conversation(chat_id)
        except Exception:
            return "product"
        if active is None:
            return "product"
        try:
            row = self._runtime_store._conn.execute(
                "SELECT * FROM runtime_events WHERE conversation_id = ? "
                "AND event_type = 'conversation.mode.switched' "
                "ORDER BY id DESC LIMIT 1",
                (active.id,),
            ).fetchone()
            if row is not None:
                import json as _json
                raw = row["payload"]
                payload = _json.loads(raw) if isinstance(raw, str) else raw
                return payload.get("to_mode", "product")
        except Exception:
            pass
        return "product"

    def _render_start_card_buttons(self, conversation_id: int) -> list[list[dict[str, str]]]:
        """Return inline keyboard buttons for the Onsite start card.

        Callback data encodes *conversation_id* (Workbench identity), not chat_id.
        """
        return [
            [{"text": "启动 Claude 现场",
              "callback_data": f"conv:{conversation_id}:start_claude_onsite"}],
            [{"text": "启动 Codex 现场",
              "callback_data": f"conv:{conversation_id}:start_codex_onsite"}],
            [{"text": "回驾驶舱",
              "callback_data": f"conv:{conversation_id}:return_cockpit"}],
        ]

    def _render_session_picker_buttons(
        self, conversation_id: int, sessions: list
    ) -> list[list[dict[str, str]]]:
        """Return inline keyboard buttons for the historical session picker.

        Callback data encodes *conversation_id* (Workbench identity), not chat_id.
        """
        from wlcodex.workbench.sessions import AgentSessionResumability

        buttons: list[list[dict[str, str]]] = []
        for s in sessions:
            agent_label = "Claude" if s.agent == "claude" else "Codex"
            row = [
                {"text": f"查看{agent_label}回顾",
                 "callback_data": f"conv:{conversation_id}:review_session:{s.source_run_id}"},
            ]
            if s.resumability is not AgentSessionResumability.SUMMARY_ONLY:
                row.append({"text": "接管现场",
                            "callback_data": f"conv:{conversation_id}:attach_session:{s.source_run_id}"})
                row.append({"text": "继续修改",
                            "callback_data": f"conv:{conversation_id}:resume_session:{s.source_run_id}"})
            else:
                row.append({"text": "从摘要新开",
                            "callback_data": f"conv:{conversation_id}:resume_from_summary:{s.source_run_id}"})
            if s.agent == "claude" and s.status == "done":
                row.append({"text": "让 Codex 验收",
                            "callback_data": f"conv:{conversation_id}:codex_verify_session:{s.source_run_id}"})
            buttons.append(row)
        buttons.append([{"text": "回驾驶舱",
                         "callback_data": f"conv:{conversation_id}:return_cockpit"}])
        return buttons

    async def _handle_session_picker_callback(
        self, update: Update, query: object,
        conversation_id: int, action: tuple[str, int],
    ) -> None:
        """Handle session picker button: review, attach, resume, verify.

        All sub-buttons encode *conversation_id* (Workbench identity), never
        chat_id.  After attach_session / resume_session the view mode is
        switched to Onsite so the next plain text routes to the terminal
        manager.  resume_session and resume_from_summary store a pending
        continuation; the first Onsite text creates the internal task/run.
        """
        kind, source_run_id = action
        chat_id = update.effective_chat.id

        if self._ledger is None:
            await self._safe_callback_answer(query, "系统未完全初始化。")
            return

        from wlcodex.workbench.sessions import AgentSessionLibrary

        library = AgentSessionLibrary(self._ledger)
        session = library.get_for_workbench(conversation_id, source_run_id)
        if session is None:
            await self._safe_callback_answer(query, "会话不存在或已被删除。")
            return

        agent_label = "Claude" if session.agent == "claude" else "Codex"

        if kind == "review_session":
            lines = [
                f"{agent_label} 现场回顾", "",
                f"摘要：{session.title}",
                f"状态：{session.status}",
                f"可继续状态：{session.user_label}",
            ]
            await self._safe_callback_answer(query, "已查看")
            await self._safe_callback_edit(
                update, query, "\n".join(lines),
                buttons=[[
                    {"text": "接管现场",
                     "callback_data": f"conv:{conversation_id}:attach_session:{source_run_id}"},
                    {"text": "回驾驶舱",
                     "callback_data": f"conv:{conversation_id}:return_cockpit"},
                ]],
            )

        elif kind == "attach_session":
            # Pure attach — no task/run, just enter Onsite.
            self._pending_continuation.pop(conversation_id, None)
            await self._attach_and_enter_onsite(
                update, query, conversation_id, chat_id, session,
            )

        elif kind == "resume_session":
            # Store pending continuation — task/run creation deferred
            # to the first Onsite text from this conversation.
            self._pending_continuation[conversation_id] = {
                "agent": session.agent,
                "internal_ref": session.internal_ref,
                "title": session.title,
                "source_run_id": source_run_id,
                "summary_only": False,
            }
            await self._attach_and_enter_onsite(
                update, query, conversation_id, chat_id, session,
            )

        elif kind == "resume_from_summary":
            self._pending_continuation[conversation_id] = {
                "agent": session.agent,
                "internal_ref": "",
                "title": session.title,
                "source_run_id": source_run_id,
                "summary_only": True,
            }
            self._record_mode_switch(
                conversation_id, chat_id, "terminal", session.agent,
            )
            await self._safe_callback_answer(query, "已准备")
            await self._safe_callback_edit(
                update, query,
                f"已准备从摘要新开 {agent_label} 现场。\n"
                f"标题：{session.title}\n\n"
                "直接发送下一条消息即可继续。",
            )

        elif kind == "codex_verify_session":
            await self._safe_callback_answer(query, "已触发验收")
            try:
                response = await self._controller.handle(
                    "/verify", {"chat_id": chat_id},
                )
                await self._safe_callback_edit(
                    update, query, response.text, response.buttons,
                )
            except Exception as exc:
                logger.exception("Codex verify from session picker failed")
                await self._safe_callback_answer(query, f"验收触发失败：{exc}")

        else:
            await self._safe_callback_answer(query, f"未知的会话操作：{kind}")

    async def _attach_and_enter_onsite(
        self, update: Update, query: object,
        conversation_id: int, chat_id: int, session: object,
    ) -> None:
        """Attach a historical session and switch view mode to Onsite."""
        if self._terminal_manager is None:
            await self._safe_callback_answer(query, "现场接管未配置。")
            return
        try:
            self._terminal_manager.attach_historical(
                conversation_id=conversation_id, session=session,
            )
        except ValueError as exc:
            await self._safe_callback_answer(query, str(exc)[:200])
            return

        # Record mode switch so subsequent plain text routes to Onsite.
        self._record_mode_switch(conversation_id, chat_id, "terminal", session.agent)

        await self._safe_callback_answer(query, "已接入")
        await self._safe_callback_edit(
            update, query,
            f"已进入接管现场，当前接入 {session.agent}。"
            f"直接发送消息即可继续。",
        )

    def _record_mode_switch(
        self, conversation_id: int, chat_id: int,
        to_mode: str, agent: str,
    ) -> None:
        """Emit conversation.mode.switched runtime event."""
        if self._runtime_store is None:
            return
        try:
            from wlcodex.runtime_events import (
                AggregateType, EventSource, EventType,
                RuntimeEvent, Visibility, now_iso,
            )
            self._runtime_store.append(RuntimeEvent(
                schema_version=1,
                event_type=EventType.CONVERSATION_MODE_SWITCHED,
                aggregate_type=AggregateType.CONVERSATION,
                aggregate_id=str(conversation_id),
                correlation_id=f"mode-switch-{conversation_id}-{to_mode}",
                source=EventSource.TELEGRAM,
                actor="user",
                visibility=Visibility.USER,
                payload={
                    "chat_id": chat_id,
                    "conversation_id": conversation_id,
                    "from_mode": "product",
                    "to_mode": to_mode,
                    "active_agent": agent,
                },
                occurred_at=now_iso(),
                conversation_id=conversation_id,
            ))
        except Exception:
            logger.debug("Failed to record mode switch", exc_info=True)

    async def _execute_pending_continuation(
        self, chat_id: int, conversation_id: int,
        active: object, text: str, pending: dict,
    ) -> bool:
        """Create hidden task + agent_run for a pending history continuation.

        Called from _handle_terminal_text on the first Onsite text after
        the user tapped 继续修改 or 从摘要新开 in the session picker.
        Returns True when the pending continuation was consumed.
        """
        from wlcodex.models import AgentRunStatus, TaskStatus

        if pending.get("summary_only"):
            agent = pending["agent"]
            cmd = "/claude" if agent == "claude" else "/codex"
            prompt = (
                f"{cmd} 从历史现场摘要继续：{pending['title']}\n\n"
                f"用户输入：{text}"
            )
            try:
                response = await self._controller.handle(
                    prompt, {"chat_id": chat_id},
                )
            except Exception:
                logger.exception("Summary-only continuation failed")
                await self.send_telegram(
                    chat_id,
                    "从摘要新开失败。请稍后重试或回驾驶舱重新开始。",
                )
                return False
            if not getattr(response, "already_rendered", False) and response.text:
                await self.send_telegram(chat_id, response.text, response.buttons)
            return True

        # 1. Create internal task (workspace lock).
        task = None
        agent_run = None
        try:
            task = self._controller._service.reserve_task(
                active.workspace_alias,
                f"继续：{pending['title']}",
                telegram_chat_id=chat_id,
            )
            self._ledger.set_conversation_active_task(conversation_id, task.id)
        except Exception:
            logger.debug("Failed to reserve task for continuation", exc_info=True)
            await self.send_telegram(
                chat_id,
                "工作区正忙，请稍后重试或使用 /new 新开工作台。",
            )
            return False

        # 2. Create agent_run linked to task.
        external_session_id = pending.get("internal_ref") or None
        agent_run = self._ledger.create_agent_run(
            conversation_id=conversation_id,
            agent=pending["agent"],
            role="continuation",
            hidden_task_id=task.id,
            external_session_id=external_session_id,
            prompt_packet_summary=f"继续历史现场：{pending['title'][:200]}",
        )
        self._ledger.update_agent_run_status(
            agent_run.id, AgentRunStatus.RUNNING.value,
        )
        self._ledger.set_task_status(
            task.id, TaskStatus.RUNNING, phase="continuation",
        )
        try:
            self._ledger.add_event(
                task.id,
                "historical_continuation_started",
                {"agent_run_id": agent_run.id},
            )
        except Exception:
            logger.debug("Unable to record continuation start", exc_info=True)

        # 3. Attach terminal session with the original session reference.
        if self._terminal_manager is not None and external_session_id:
            try:
                strategy = (
                    "stream_json" if pending["agent"] == "claude" else "app_server"
                )
                session_ref = self._terminal_manager.attach(
                    conversation_id=conversation_id,
                    agent=pending["agent"],
                    strategy=strategy,
                    external_session_id=external_session_id,
                )
            except Exception:
                logger.debug("Terminal attach for continuation failed", exc_info=True)
                session_ref = None
        else:
            session_ref = (
                self._terminal_manager.active_for_conversation(conversation_id)
                if self._terminal_manager is not None else None
            )

        # 4. Send user text as resume input.
        try:
            if session_ref is not None:
                await self._terminal_manager.send_input(session_ref, text)
            else:
                raise RuntimeError("no terminal session for continuation")
        except Exception as exc:
            logger.exception("Resume input failed")
            self._ledger.update_agent_run_status(
                agent_run.id,
                AgentRunStatus.FAILED.value,
                completion_summary=str(exc)[:2000],
            )
            self._ledger.set_task_status(
                task.id,
                TaskStatus.FAILED,
                phase="continuation",
                error=str(exc)[:240],
            )
            try:
                self._ledger.add_event(
                    task.id,
                    "historical_continuation_failed",
                    {"agent_run_id": agent_run.id, "error": str(exc)[:500]},
                )
            except Exception:
                logger.debug("Unable to record continuation failure", exc_info=True)
            await self.send_telegram(
                chat_id,
                "发送输入失败。使用 /terminal 重新接入后再试。",
            )
            return True

        # 5. Mark the dispatch ticket terminal so it does not hold the lock.
        self._ledger.update_agent_run_status(
            agent_run.id,
            AgentRunStatus.DONE.value,
            completion_summary="历史现场继续输入已发送",
        )
        self._ledger.set_task_status(
            task.id,
            TaskStatus.DONE,
            phase="continuation",
            summary="历史现场继续输入已发送",
        )
        try:
            self._ledger.add_event(
                task.id,
                "historical_continuation_completed",
                {"agent_run_id": agent_run.id},
            )
        except Exception:
            logger.debug("Unable to record continuation completion", exc_info=True)
        return True

    async def _handle_terminal_text(self, chat_id: int, text: str) -> None:
        """Route text to the terminal input path when in terminal mode.

        If a terminal manager is wired and an active session exists for the
        current conversation, the text is sent as terminal input and a
        ``terminal.session.input.sent`` runtime event is recorded.

        If no terminal manager or no active session is available, this must
        NOT silently fall through to the product orchestrator.  It sends an
        actionable hint so the user can attach a session.
        """
        conversation_id = None
        try:
            active = self._ledger.get_active_conversation(chat_id)
            conversation_id = active.id if active is not None else None
        except Exception:
            pass

        # BLOCKER B: pending historical continuation — create task/run
        # on the first Onsite text, then send resume input.
        if conversation_id is not None and conversation_id in self._pending_continuation:
            pending = self._pending_continuation[conversation_id]
            consumed = await self._execute_pending_continuation(
                chat_id, conversation_id, active, text, pending,
            )
            if consumed:
                self._pending_continuation.pop(conversation_id, None)
            return

        # Try to route through the terminal manager when available.
        if self._terminal_manager is not None and conversation_id is not None:
            session_ref = self._terminal_manager.active_for_conversation(
                conversation_id
            )
            if session_ref is not None:
                try:
                    result = await self._terminal_manager.send_input(session_ref, text)
                except ValueError as exc:
                    # No active turn / no session — actionable error
                    await self.send_telegram(
                        chat_id,
                        f"无法发送现场输入：{exc}"
                    )
                    return
                except Exception:
                    logger.exception(
                        "Terminal manager send_input failed: conv=%d", conversation_id
                    )
                    await self.send_telegram(
                        chat_id,
                        "发送现场输入失败。现场会话可能已断开，"
                        "请使用 /terminal 重新连接。"
                    )
                    return

                # Display Claude terminal output (Codex output arrives
                # through the app-server WebSocket notification channel).
                if result is not None and hasattr(result, "text"):
                    output = result.text
                    if output:
                        # Truncate to a reasonable length for Telegram
                        max_len = getattr(
                            getattr(self._config, "terminal", None),
                            "max_frame_chars", 3500,
                        )
                        display = output if len(output) <= max_len else (
                            output[:max_len] + "\n\n... (输出已截断)"
                        )
                        await self.send_telegram(chat_id, display)

                # Record terminal.session.input.sent runtime event
                if self._runtime_store is not None:
                    try:
                        from wlcodex.runtime_events import (
                            AggregateType,
                            EventSource,
                            EventType,
                            RuntimeEvent,
                            Visibility,
                            now_iso,
                        )

                        self._runtime_store.append(RuntimeEvent(
                            schema_version=1,
                            event_type=EventType.TERMINAL_SESSION_INPUT_SENT,
                            aggregate_type=AggregateType.CONVERSATION,
                            aggregate_id=str(conversation_id),
                            correlation_id=f"terminal-input-{chat_id}",
                            source=EventSource.TELEGRAM,
                            actor="user",
                            visibility=Visibility.USER,
                            payload={
                                "chat_id": chat_id,
                                "conversation_id": conversation_id,
                                "agent": session_ref.agent,
                                "external_session_id": session_ref.external_session_id,
                                "text_preview": text[:500],
                                "text_length": len(text),
                            },
                            occurred_at=now_iso(),
                            conversation_id=conversation_id,
                        ))
                    except Exception:
                        logger.debug(
                            "Failed to append terminal.session.input.sent event",
                            exc_info=True,
                        )
                return

        # No terminal manager or no active session — never a dead end.
        cid = conversation_id if conversation_id is not None else chat_id
        await self.send_telegram(
            chat_id,
            "当前没有可接管的现场。\n\n"
            "你可以：",
            buttons=self._render_start_card_buttons(cid),
        )

    async def _apply_mode_switch(
        self, update: Update, command: object
    ) -> None:
        """Record a conversation.mode.switched event and send confirmation.

        Must NOT create a new conversation or start a new task.
        """
        chat_id = update.effective_chat.id
        user = update.effective_user

        active = self._ledger.get_active_conversation(chat_id)
        conversation_id = active.id if active is not None else None

        from_mode = "product"
        to_mode = command.mode
        agent = getattr(command, "agent", "")

        # Resolve the terminal agent for bare /terminal: explicit > config default.
        # Must happen before the mode-switch event so the recorded active_agent is correct.
        if to_mode == "terminal" and not agent:
            agent = getattr(
                getattr(self._config, "terminal", None),
                "default_agent",
                "claude",
            )

        # Determine current mode from stored state
        if self._runtime_store is not None and conversation_id is not None:
            try:
                row = self._runtime_store._conn.execute(
                    "SELECT * FROM runtime_events WHERE conversation_id = ? "
                    "AND event_type = 'conversation.mode.switched' "
                    "ORDER BY id DESC LIMIT 1",
                    (conversation_id,),
                ).fetchone()
                if row is not None:
                    import json as _json
                    raw = row["payload"]
                    p = _json.loads(raw) if isinstance(raw, str) else raw
                    from_mode = p.get("to_mode", "product")
            except Exception:
                pass

        # Record the mode switch event
        if self._runtime_store is not None:
            try:
                from wlcodex.runtime_events import (
                    AggregateType,
                    EventSource,
                    EventType,
                    RuntimeEvent,
                    Visibility,
                    now_iso,
                )

                self._runtime_store.append(RuntimeEvent(
                    schema_version=1,
                    event_type=EventType.CONVERSATION_MODE_SWITCHED,
                    aggregate_type=AggregateType.CONVERSATION,
                    aggregate_id=str(conversation_id or chat_id),
                    correlation_id=f"mode-switch-{update.update_id}",
                    source=EventSource.TELEGRAM,
                    actor="user",
                    visibility=Visibility.USER,
                    payload={
                        "chat_id": chat_id,
                        "conversation_id": conversation_id,
                        "from_mode": from_mode,
                        "to_mode": to_mode,
                        "active_agent": agent,
                        "user_id": user.id if user is not None else None,
                        "telegram_update_id": update.update_id,
                    },
                    occurred_at=now_iso(),
                    conversation_id=conversation_id,
                ))
            except Exception:
                logger.debug("Failed to append mode switch event", exc_info=True)

        # Send confirmation + attach terminal session when applicable
        if to_mode == "product":
            await self.send_telegram(
                chat_id,
                "已回到驾驶舱。现场仍在运行，我会继续用摘要跟进。"
            )
        elif to_mode == "terminal":
            attached = False
            if agent and self._terminal_manager is not None and conversation_id is not None:
                # Check if conversation already has an active session for this agent
                existing = self._terminal_manager.active_for_conversation(conversation_id)
                if existing is not None and existing.agent == agent:
                    attached = True
                else:
                    ext_id = self._find_external_session_id(conversation_id, agent)
                    if ext_id:
                        strategy = (
                            "stream_json" if agent == "claude" else "app_server"
                        )
                        try:
                            ref = self._terminal_manager.attach(
                                conversation_id=conversation_id,
                                agent=agent,
                                strategy=strategy,
                                external_session_id=ext_id,
                            )
                            attached = True
                            # Record terminal.session.attached runtime event
                            if self._runtime_store is not None:
                                try:
                                    from wlcodex.runtime_events import (
                                        AggregateType,
                                        EventSource,
                                        EventType,
                                        RuntimeEvent,
                                        Visibility,
                                        now_iso,
                                    )

                                    self._runtime_store.append(RuntimeEvent(
                                        schema_version=1,
                                        event_type=EventType.TERMINAL_SESSION_ATTACHED,
                                        aggregate_type=AggregateType.CONVERSATION,
                                        aggregate_id=str(conversation_id),
                                        correlation_id=f"terminal-attach-{update.update_id}",
                                        source=EventSource.TELEGRAM,
                                        actor="user",
                                        visibility=Visibility.USER,
                                        payload={
                                            "chat_id": chat_id,
                                            "conversation_id": conversation_id,
                                            "agent": agent,
                                            "strategy": strategy,
                                            "external_session_id": ext_id,
                                            "status": "attached",
                                        },
                                        occurred_at=now_iso(),
                                        conversation_id=conversation_id,
                                    ))
                                except Exception:
                                    logger.debug(
                                        "Failed to append terminal.session.attached event",
                                        exc_info=True,
                                    )
                        except Exception:
                            logger.exception(
                                "Terminal session attach failed: conv=%d agent=%s",
                                conversation_id, agent,
                            )

            if attached:
                await self.send_telegram(
                    chat_id,
                    f"已进入接管现场，当前接入 {agent}。"
                )
            else:
                # Check for historical sessions before showing start card.
                sessions = []
                if conversation_id is not None and self._ledger is not None:
                    try:
                        from wlcodex.workbench.sessions import AgentSessionLibrary
                        library = AgentSessionLibrary(self._ledger)
                        sessions = library.list_for_workbench(conversation_id)
                    except Exception:
                        logger.debug("Failed to list historical sessions", exc_info=True)

                if sessions:
                    from wlcodex.workbench.rendering import render_session_library
                    text = render_session_library(sessions)
                    cid = conversation_id if conversation_id is not None else chat_id
                    buttons = self._render_session_picker_buttons(cid, sessions)
                    await self.send_telegram(chat_id, text, buttons=buttons)
                else:
                    await self.send_telegram(
                        chat_id,
                        "当前没有可接管的现场。\n\n"
                        "你可以：",
                        buttons=self._render_start_card_buttons(
                            conversation_id if conversation_id is not None else chat_id
                        ),
                    )

    # --- New conversation handlers ---

    async def new_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await self.send_telegram(update.effective_chat.id, response.text)

    async def codex_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        chat_id = update.effective_chat.id
        typing_task = await self._start_typing(chat_id)
        try:
            response = await self._controller.handle(
                update.effective_message.text, _ctx(update)
            )
        finally:
            typing_task.cancel()
        # Streaming path: renderer already handled all output
        if response.already_rendered:
            return
        sent = await self._reply_with_buttons(update, response.text, response.buttons)
        await self._track_status_from_response(response.text, chat_id, sent.message_id)

    async def claude_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        chat_id = update.effective_chat.id
        typing_task = await self._start_typing(chat_id)
        try:
            response = await self._controller.handle(
                update.effective_message.text, _ctx(update)
            )
        finally:
            typing_task.cancel()

        # Streaming path: renderer already handled all output
        if response.already_rendered:
            return

        # Use streaming renderer when buttons are present (Claude completed)
        if response.buttons:
            renderer = self.create_streaming_renderer(chat_id)
            try:
                await renderer.start(chat_id, response.text)
                await renderer.finish(buttons=response.buttons)
            except Exception as exc:
                logger.warning(
                    "Claude streaming renderer failed: chat_id=%s exc=%s", chat_id, exc
                )
                await self.send_telegram(chat_id, response.text)
        else:
            await self.send_telegram(chat_id, response.text)

    async def auto_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        chat_id = update.effective_chat.id

        interaction = getattr(self._config, "interaction", None)
        profile_name = getattr(interaction, "profile", "legacy")

        # Natural profile: streaming handles everything, no ACK needed
        if profile_name == "natural":
            typing_task = await self._start_typing(chat_id)
            try:
                response = await self._controller.handle(
                    update.effective_message.text, _ctx(update)
                )
            finally:
                typing_task.cancel()
            if response.already_rendered:
                return
            await self.send_telegram(chat_id, response.text, response.buttons)
            return

        # Legacy: Send ACK immediately so the user never sees "no response".
        ack_msg_id = await self.send_telegram(
            chat_id, "正在分析你的需求，请稍候..."
        )

        typing_task = await self._start_typing(chat_id)
        try:
            response = await self._controller.handle(
                update.effective_message.text, _ctx(update)
            )
        finally:
            typing_task.cancel()

        # Try to edit the ACK message with results; fall back to new message.
        await self._edit_or_send_result(
            chat_id, ack_msg_id, response.text, response.buttons
        )

    async def verify_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        chat_id = update.effective_chat.id
        typing_task = await self._start_typing(chat_id)
        try:
            response = await self._controller.handle(
                update.effective_message.text, _ctx(update)
            )
        finally:
            typing_task.cancel()
        await self.send_telegram(chat_id, response.text)

    async def stop_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle("/stop", _ctx(update))
        await self.send_telegram(update.effective_chat.id, response.text)

    async def switch_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await self.send_telegram(update.effective_chat.id, response.text)

    async def model_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await self._reply_with_buttons(update, response.text, response.buttons)

    async def claude_permission_cmd(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await self._reply_with_buttons(update, response.text, response.buttons)

    async def conversation_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle non-command text as a conversation message.

        Routes by active surface mode:
          - product  -> existing conversation controller
          - terminal -> terminal session input (never the product orchestrator)
        """
        if not self._guard(update):
            return
        text = update.effective_message.text
        chat_id = update.effective_chat.id

        # Check active surface mode so terminal input never calls product orchestrator
        active_mode = self._get_active_surface_mode(chat_id)
        if active_mode == "terminal":
            await self._handle_terminal_text(chat_id, text)
            return

        interaction = getattr(self._config, "interaction", None)
        profile_name = getattr(interaction, "profile", "legacy")

        if profile_name == "natural":
            typing_task = await self._start_typing(chat_id)
            try:
                response = await self._controller.handle_conversation_text(
                    text, _ctx(update)
                )
            finally:
                typing_task.cancel()
            # Streaming path: renderer already handled all output
            if response.already_rendered:
                return
            await self.send_telegram(chat_id, response.text, response.buttons)
            return

        # Send ACK immediately — the controller may run a long orchestration.
        ack_msg_id = await self.send_telegram(
            chat_id, "正在处理你的消息，请稍候..."
        )

        typing_task = await self._start_typing(chat_id)
        try:
            response = await self._controller.handle_conversation_text(text, _ctx(update))
        finally:
            typing_task.cancel()

        await self._edit_or_send_result(
            chat_id, ack_msg_id, response.text, response.buttons
        )

    # --- Result delivery helper ---

    async def _edit_or_send_result(
        self,
        chat_id: int,
        ack_msg_id: int,
        text: str,
        buttons: list[list[dict[str, str]]] | None = None,
    ) -> None:
        """Edit the ACK message with results, or send a new message if edit fails."""
        if ack_msg_id != SEND_FAILED:
            try:
                await self.edit_telegram(chat_id, ack_msg_id, text, buttons=buttons)
                return
            except Exception:
                pass

        # ACK never sent or edit failed — send a fresh eventized message.
        await self.send_telegram(chat_id, text, buttons)

    # --- Callback routing ---

    async def callback_router(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Route callbacks by prefix: approval:*, waiting:*, worktree_done:*, settings:*."""
        query = update.callback_query
        if query is None:
            return

        if not self._guard(update):
            await self._safe_callback_answer(query, "未授权。")
            return

        data = query.data or ""
        self._append_telegram_callback_event(update, data)

        if data.startswith("approval:"):
            await self._approval_callback_impl(update, query, data)
        elif data.startswith("waiting:"):
            await self._waiting_callback_impl(update, query, data)
        elif data.startswith("worktree_done:"):
            await self._worktree_done_callback_impl(update, query, data)
        elif data.startswith("conv:"):
            await self._conversation_callback_impl(update, query, data)
        elif data.startswith("settings:"):
            await self._settings_callback_impl(update, query, data)
        elif data.startswith("busy_"):
            await self._workspace_busy_callback_impl(update, query, data)
        else:
            await self._safe_callback_answer(query, "未知回调类型。")

    async def _settings_callback_impl(
        self, update: Update, query: object, data: str
    ) -> None:
        parts = data.split(":", 2)
        if len(parts) < 2:
            await self._safe_callback_answer(query, "无效的设置回调数据。")
            return

        sub = parts[1]
        if sub == "claude_permission" and len(parts) == 3:
            controller_cmd = f"/claude_mode {parts[2]}"
        elif sub == "exec_mode" and len(parts) == 3:
            controller_cmd = f"/exec_mode {parts[2]}"
        elif sub == "model":
            controller_cmd = "/model"
        elif sub == "workspace":
            controller_cmd = "/switch"
        else:
            await self._safe_callback_answer(query, "无效的设置回调数据。")
            return

        try:
            response = await self._controller.handle(
                controller_cmd,
                _ctx(update),
            )
            await self._safe_callback_answer(query, "已切换")
            await self._safe_callback_edit(
                update, query, response.text, response.buttons
            )
        except Exception as exc:
            logger.exception("Settings callback error")
            await self._safe_callback_answer(query, f"错误：{exc}")

    async def _conversation_callback_impl(
        self, update: Update, query: object, data: str
    ) -> None:
        from wlcodex.conversation_callback import decode_conversation_callback

        callback = decode_conversation_callback(data)
        if callback is None:
            await self._safe_callback_answer(query, "无效的对话回调数据。")
            return

        # Intercept session-picker actions before delegating to controller.
        session_action = _parse_session_action(callback.action)
        if session_action is not None:
            await self._handle_session_picker_callback(
                update, query, callback.conversation_id, session_action,
            )
            return

        try:
            response = await self._controller.handle_conversation_callback(callback)
            await self._safe_callback_answer(query, "完成")
            await self._safe_callback_edit(
                update, query, response.text, response.buttons
            )
        except Exception as exc:
            logger.exception("Conversation callback error")
            await self._safe_callback_answer(query, f"错误：{exc}")

    async def _workspace_busy_callback_impl(
        self, update: Update, query: object, data: str
    ) -> None:
        from wlcodex.conversation_state_machine import decode_busy_callback

        decoded = decode_busy_callback(data)
        if decoded is None:
            await self._safe_callback_answer(query, "无效的工作区忙回调数据。")
            return

        action, conversation_id = decoded
        try:
            response = await self._controller.handle_workspace_busy_callback(
                action, conversation_id
            )
            await self._safe_callback_answer(query, "完成")
            await self._safe_callback_edit(
                update, query, response.text, response.buttons
            )
        except Exception as exc:
            logger.exception("Workspace busy callback error")
            await self._safe_callback_answer(query, f"错误：{exc}")

    async def _poller_error_handler(
        self, update: object, context: object
    ) -> None:
        """Global error handler for Telegram polling resilience.

        Logs the error, emits a runtime event, and never crashes the poller.
        """
        exc = context.error if hasattr(context, "error") else None
        if exc is None:
            return
        logger.error("Telegram poller error: %s", exc)

        if self._runtime_store is not None:
            try:
                from wlcodex.runtime_events import (
                    AggregateType, EventSource, EventType, RuntimeEvent,
                    Visibility, now_iso,
                )
                self._runtime_store.append(RuntimeEvent(
                    schema_version=1,
                    event_type=EventType.TELEGRAM_POLLER_ERROR,
                    aggregate_type=AggregateType.SYSTEM,
                    aggregate_id="telegram-poller",
                    correlation_id="poller-error",
                    source=EventSource.SYSTEM,
                    actor="telegram_poller",
                    visibility=Visibility.INTERNAL,
                    payload={
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    },
                    occurred_at=now_iso(),
                ))
            except Exception:
                logger.debug("Failed to emit poller error event", exc_info=True)

    async def _approval_callback_impl(
        self, update: Update, query: object, data: str
    ) -> None:
        from wlcodex.approval import decode_approval_callback

        callback = decode_approval_callback(data)
        if callback is None:
            await self._safe_callback_answer(query, "无效的审批回调数据。")
            return

        # Check if this approval has been superseded by new user context.
        # Must scope by conversation AND time: only superseded events that
        # occurred AFTER this approval was created.
        if self._runtime_store is not None:
            try:
                approvals_conn = self._runtime_store._conn
                # Get this approval's conversation_id and created_at
                approval_row = approvals_conn.execute(
                    """
                    SELECT task_id, created_at FROM approval_requests WHERE id = ?
                    """,
                    (callback.approval_id,),
                ).fetchone()
                if approval_row is not None:
                    task_id = approval_row["task_id"]
                    approval_created_at = str(approval_row["created_at"])
                    # Find conversation_id from tasks
                    task_row = approvals_conn.execute(
                        "SELECT telegram_chat_id FROM tasks WHERE id = ?",
                        (task_id,),
                    ).fetchone()
                    if task_row is not None:
                        chat_id = task_row["telegram_chat_id"]
                        # Find all active conversations for this chat
                        conv_row = approvals_conn.execute(
                            """
                            SELECT id FROM conversation_sessions
                            WHERE chat_id = ? AND archived_at IS NULL
                            ORDER BY updated_at DESC LIMIT 1
                            """,
                            (chat_id,),
                        ).fetchone()
                        if conv_row is not None:
                            conversation_id = conv_row["id"]
                            # Check for supersession events for THIS conversation
                            # that happened AFTER this approval was created
                            superseded = approvals_conn.execute(
                                """
                                SELECT 1 FROM runtime_events
                                WHERE event_type = 'approval.superseded'
                                  AND conversation_id = ?
                                  AND occurred_at >= ?
                                LIMIT 1
                                """,
                                (conversation_id, approval_created_at),
                            ).fetchone()
                            if superseded is not None:
                                from wlcodex.runtime_events import (
                                    AggregateType, EventSource, EventType,
                                    RuntimeEvent, Visibility, now_iso,
                                )
                                self._runtime_store.append(RuntimeEvent(
                                    schema_version=1,
                                    event_type=EventType.APPROVAL_STALE_BUTTON_IGNORED,
                                    aggregate_type=AggregateType.APPROVAL,
                                    aggregate_id=str(callback.approval_id),
                                    correlation_id="stale-check",
                                    source=EventSource.TELEGRAM,
                                    actor="telegram_bot",
                                    visibility=Visibility.INTERNAL,
                                    payload={"reason": "approval_superseded",
                                             "approval_id": callback.approval_id,
                                             "conversation_id": conversation_id},
                                    occurred_at=now_iso(),
                                ))
                                await self._safe_callback_answer(query, "该审批已被新的用户上下文取代，请使用新的审批按钮。")
                                return
            except Exception:
                logger.debug("Supersession check failed (non-fatal)", exc_info=True)

        try:
            msg = await self._approval.resolve_callback(
                callback, self._controller._backend, self._ledger
            )
            # Approval decision is a protocol fact — answer/edit failures
            # must be recorded but MUST NOT undo the decision.
            await self._safe_callback_answer(query, "完成")
            await self._safe_callback_edit(
                update,
                query,
                f"{query.message.text}\n\n处理结果：{msg}"
                if query.message else msg,
            )
        except Exception as exc:
            logger.exception("Approval callback error")
            await self._safe_callback_answer(query, f"错误：{exc}")

    async def _waiting_callback_impl(
        self, update: Update, query: object, data: str
    ) -> None:
        from wlcodex.waiting_callback import decode_waiting_callback

        callback = decode_waiting_callback(data)
        if callback is None:
            await self._safe_callback_answer(query, "无效的等待回调数据。")
            return

        try:
            response = await self._controller.handle_waiting_callback(callback)
            await self._safe_callback_answer(query, "完成")
            await self._safe_callback_edit(
                update, query, response.text, response.buttons
            )
        except Exception as exc:
            logger.exception("Waiting callback error")
            await self._safe_callback_answer(query, f"错误：{exc}")

    async def _worktree_done_callback_impl(
        self, update: Update, query: object, data: str
    ) -> None:
        from wlcodex.waiting_callback import decode_worktree_done_callback

        callback = decode_worktree_done_callback(data)
        if callback is None:
            await self._safe_callback_answer(query, "无效的 worktree 回调数据。")
            return

        try:
            response = await self._controller.handle_worktree_done_callback(callback)
            await self._safe_callback_answer(query, "完成")
            await self._safe_callback_edit(
                update, query, response.text, response.buttons
            )
        except Exception as exc:
            logger.exception("Worktree done callback error")
            await self._safe_callback_answer(query, f"错误：{exc}")

    # --- Legacy approval callback (kept for backward compat) ---

    async def approval_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.callback_router(update, context)

    async def _track_status_from_response(
        self, text: str, chat_id: int, message_id: int
    ) -> None:
        if message_id == SEND_FAILED:
            return
        import re
        m = re.search(r"(?:任务|Task) #(\d+)", text)
        if m:
            await self._store_status_message(int(m.group(1)), chat_id, message_id)


# --- Application builder ---


def _parse_session_action(action: str) -> tuple[str, int] | None:
    """Parse compound session action like 'review_session:123'.

    Returns (kind, source_run_id) or None if not a session action.
    Module-level helper used by WlCodexHandlers._conversation_callback_impl.
    """
    SESSION_KINDS = {
        "review_session", "attach_session", "resume_session",
        "resume_from_summary", "codex_verify_session",
    }
    for kind in SESSION_KINDS:
        prefix = f"{kind}:"
        if action.startswith(prefix):
            try:
                source_run_id = int(action[len(prefix):])
                return (kind, source_run_id)
            except ValueError:
                return None
    return None


def build_application(
    config: AppConfig,
    token: str,
    controller: CommandController | None = None,
    ledger: Ledger | None = None,
    approval_service: object = None,
    runtime_event_store: object | None = None,
    outbox: object | None = None,
    terminal_manager: object | None = None,
) -> tuple[Application, WlCodexHandlers | None]:
    # Bot API HTTP timeouts.
    # Long-polling HTTP timeouts (getUpdates).
    application = (
        Application.builder()
        .token(token)
        .concurrent_updates(8)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(5.0)
        .get_updates_connect_timeout(30.0)
        .get_updates_read_timeout(60.0)
        .get_updates_write_timeout(30.0)
        .get_updates_pool_timeout(5.0)
        .build()
    )

    if controller is not None and ledger is not None:
        handlers = WlCodexHandlers(
            config, controller, ledger, approval_service, application.bot,
            runtime_event_store=runtime_event_store,
            outbox=outbox,
            terminal_manager=terminal_manager,
        )
    else:
        application.add_handler(CommandHandler("start", _skeleton_start))
        return application, None

    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("help", handlers.help_cmd))
    application.add_handler(CommandHandler("settings", handlers.settings_cmd))
    application.add_handler(CommandHandler("task", handlers.task))
    application.add_handler(CommandHandler("tasks", handlers.tasks_list))
    application.add_handler(CommandHandler("status", handlers.status))
    application.add_handler(CommandHandler("trace", handlers.task))
    application.add_handler(CommandHandler("continue", handlers.continue_cmd))
    application.add_handler(CommandHandler("steer", handlers.steer))
    application.add_handler(CommandHandler("tail", handlers.tail))
    application.add_handler(CommandHandler("events", handlers.events))
    application.add_handler(CommandHandler("diff", handlers.diff))
    application.add_handler(CommandHandler("files", handlers.files))
    application.add_handler(CommandHandler("pause", handlers.pause))
    application.add_handler(CommandHandler("abort", handlers.abort))
    application.add_handler(CommandHandler("archive", handlers.archive))
    application.add_handler(CommandHandler("fork", handlers.fork))
    application.add_handler(CommandHandler("codex_sessions", handlers.codex_sessions))
    application.add_handler(CommandHandler("sessions", handlers.codex_sessions))
    application.add_handler(CommandHandler("health", handlers.health))

    # Dual-surface mode commands
    application.add_handler(CommandHandler("mode", handlers.mode_cmd))
    application.add_handler(CommandHandler("product", handlers.product_cmd))
    application.add_handler(CommandHandler("terminal", handlers.terminal_cmd))

    # New conversation commands
    application.add_handler(CommandHandler("new", handlers.new_cmd))
    application.add_handler(CommandHandler("codex", handlers.codex_cmd))
    application.add_handler(CommandHandler("claude", handlers.claude_cmd))
    application.add_handler(CommandHandler("auto", handlers.auto_cmd))
    application.add_handler(CommandHandler("stop", handlers.stop_cmd))
    application.add_handler(CommandHandler("switch", handlers.switch_cmd))
    application.add_handler(CommandHandler("model", handlers.model_cmd))
    application.add_handler(CommandHandler("claude_mode", handlers.claude_permission_cmd))
    application.add_handler(CommandHandler("claude_permission", handlers.claude_permission_cmd))
    application.add_handler(CommandHandler("verify", handlers.verify_cmd))

    # Non-command text → conversation
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.conversation_text)
    )

    application.add_handler(CallbackQueryHandler(handlers.callback_router))

    # Register global error handler for polling resilience.
    application.add_error_handler(handlers._poller_error_handler)

    return application, handlers


async def _skeleton_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "WLCodex 已在线。使用 /task <workspace> <prompt> 创建任务，或用 /tasks 查看任务。"
    )


def _ctx(update: Update) -> dict[str, Any]:
    return {
        "chat_id": update.effective_chat.id,
        "user_id": update.effective_user.id,
    }
