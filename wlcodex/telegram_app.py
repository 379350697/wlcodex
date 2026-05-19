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
    ) -> None:
        self._config = config
        self._controller = controller
        self._ledger = ledger
        self._approval = approval_service
        self._bot = bot
        self._runtime_store = runtime_event_store
        self._outbox = outbox

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
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await self.send_telegram(update.effective_chat.id, response.text)

    async def health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle("/health", _ctx(update))
        await self.send_telegram(update.effective_chat.id, response.text)

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
        """Handle non-command text as a conversation message."""
        if not self._guard(update):
            return
        text = update.effective_message.text
        chat_id = update.effective_chat.id

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
        if len(parts) != 3 or parts[1] != "claude_permission":
            await self._safe_callback_answer(query, "无效的设置回调数据。")
            return

        try:
            response = await self._controller.handle(
                f"/claude_mode {parts[2]}",
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


def build_application(
    config: AppConfig,
    token: str,
    controller: CommandController | None = None,
    ledger: Ledger | None = None,
    approval_service: object = None,
    runtime_event_store: object | None = None,
    outbox: object | None = None,
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
        )
    else:
        application.add_handler(CommandHandler("start", _skeleton_start))
        return application, None

    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("help", handlers.help_cmd))
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
