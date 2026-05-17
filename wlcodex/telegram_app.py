from __future__ import annotations

import asyncio
import logging
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
    ) -> None:
        self._config = config
        self._controller = controller
        self._ledger = ledger
        self._approval = approval_service
        self._bot = bot

    # --- Auth guard ---

    def _guard(self, update: Update) -> bool:
        ok = ensure_authorized(update, self._config.telegram.allowed_user_ids)
        if ok and update.effective_user:
            self._ledger.record_telegram_update(
                update_id=update.update_id,
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id if update.effective_chat else 0,
                update_type=(
                    update.effective_message.text
                    if update.effective_message and update.effective_message.text
                    else "callback"
                ),
                allowed=True,
            )
        elif not ok and update.effective_user:
            self._ledger.record_telegram_update(
                update_id=update.update_id,
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id if update.effective_chat else 0,
                update_type="rejected",
                allowed=False,
            )
        return ok

    async def _store_status_message(
        self, task_id: int, chat_id: int, message_id: int
    ) -> None:
        self._ledger.set_status_message(task_id, chat_id, message_id)

    async def _reply_with_buttons(
        self, update: Update, text: str, buttons: list[list[dict[str, str]]]
    ) -> object:
        """Reply with inline keyboard buttons if any."""
        if buttons:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            keyboard = [
                [InlineKeyboardButton(b["text"], callback_data=b["callback_data"])
                 for b in row]
                for row in buttons
            ]
            return await update.effective_message.reply_text(
                text, reply_markup=InlineKeyboardMarkup(keyboard)
            )
        return await update.effective_message.reply_text(text)

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
        """Send a message via the bot. Returns message_id, or SEND_FAILED (-1)
        on transient network errors so handlers don't crash."""
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
            msg = await self._bot.send_message(
                chat_id=chat_id, text=text, reply_markup=reply_markup
            )
            return msg.message_id
        except (TimedOut, NetworkError) as exc:
            logger.warning(
                "Telegram send timed out: chat_id=%s text_len=%d exc=%s",
                chat_id, len(text), exc,
            )
            return SEND_FAILED
        except TelegramError as exc:
            if _is_telegram_network_error(exc):
                logger.warning(
                    "Telegram send network error: chat_id=%s text_len=%d exc=%s",
                    chat_id, len(text), exc,
                )
                return SEND_FAILED
            raise

    async def edit_telegram(
        self, chat_id: int, message_id: int, text: str,
        buttons: list[list[dict[str, str]]] | None = None,
    ) -> None:
        """Edit an existing message, optionally with inline keyboard buttons.

        Network errors are caught and logged; they do NOT fall back to
        send_message (which would likely also fail on the same network).
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

        try:
            await self._bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text,
                reply_markup=reply_markup,
            )
        except Exception as exc:
            if _is_message_not_modified_error(exc):
                try:
                    await self._bot.edit_message_text(
                        chat_id=chat_id, message_id=message_id,
                        text=text + "​",
                        reply_markup=reply_markup,
                    )
                except Exception:
                    pass
                return

            if _is_telegram_network_error(exc):
                logger.warning(
                    "Telegram edit network error: chat_id=%s msg_id=%s exc=%s",
                    chat_id, message_id, exc,
                )
                return

            logger.debug("Failed to edit message %d, sending new one", message_id)
            try:
                new_msg = await self._bot.send_message(
                    chat_id=chat_id, text=text, reply_markup=reply_markup,
                )
            except Exception as send_exc:
                logger.warning(
                    "Telegram edit fallback send failed: chat_id=%s exc=%s",
                    chat_id, send_exc,
                )
                return
            for task in self._ledger.list_tasks(limit=50, include_archived=True):
                if task.telegram_status_message_id == message_id:
                    self._ledger.set_status_message(task.id, chat_id, new_msg.message_id)

    # --- Handlers ---

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        profile_name = getattr(
            getattr(self._config, "interaction", None), "profile", "legacy"
        )
        from wlcodex.status import render_conversation_help
        await update.effective_message.reply_text(render_conversation_help(profile=profile_name))

    async def help_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        profile_name = getattr(
            getattr(self._config, "interaction", None), "profile", "legacy"
        )
        from wlcodex.status import render_conversation_help
        await update.effective_message.reply_text(render_conversation_help(profile=profile_name))

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
                    if m:
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
        await update.effective_message.reply_text(response.text)

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle("/tasks", _ctx(update))
        await update.effective_message.reply_text(response.text)

    async def continue_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        sent = await update.effective_message.reply_text(response.text)
        # Track status message for continue too
        await self._track_status_from_response(response.text, update.effective_chat.id, sent.message_id)

    async def steer(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await update.effective_message.reply_text(response.text)

    async def tail(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await update.effective_message.reply_text(response.text)

    async def events(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await update.effective_message.reply_text(response.text)

    async def diff(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await update.effective_message.reply_text(response.text)

    async def files(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await update.effective_message.reply_text(response.text)

    async def pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await update.effective_message.reply_text(response.text)

    async def abort(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await update.effective_message.reply_text(response.text)

    async def archive(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await update.effective_message.reply_text(response.text)

    async def fork(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        sent = await update.effective_message.reply_text(response.text)
        await self._track_status_from_response(response.text, update.effective_chat.id, sent.message_id)

    async def codex_sessions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await update.effective_message.reply_text(response.text)

    async def health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle("/health", _ctx(update))
        await update.effective_message.reply_text(response.text)

    # --- New conversation handlers ---

    async def new_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await update.effective_message.reply_text(response.text)

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
            await update.effective_message.reply_text(response.text)

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
        await update.effective_message.reply_text(response.text)

    async def stop_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle("/stop", _ctx(update))
        await update.effective_message.reply_text(response.text)

    async def switch_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        response = await self._controller.handle(
            update.effective_message.text, _ctx(update)
        )
        await update.effective_message.reply_text(response.text)

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

        # ACK never sent or edit failed — send a fresh message.
        if buttons:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            keyboard = [
                [InlineKeyboardButton(b["text"], callback_data=b["callback_data"])
                 for b in row]
                for row in buttons
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            try:
                await self._bot.send_message(
                    chat_id=chat_id, text=text, reply_markup=reply_markup,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to send result message: chat_id=%s exc=%s", chat_id, exc
                )
        else:
            await self.send_telegram(chat_id, text)

    # --- Callback routing ---

    async def callback_router(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Route callbacks by prefix: approval:*, waiting:*, worktree_done:*, settings:*."""
        query = update.callback_query
        if query is None:
            return

        if not self._guard(update):
            await query.answer("未授权。")
            return

        data = query.data or ""

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
        else:
            await query.answer("未知回调类型。")

    async def _settings_callback_impl(
        self, update: Update, query: object, data: str
    ) -> None:
        parts = data.split(":", 2)
        if len(parts) != 3 or parts[1] != "claude_permission":
            await query.answer("无效的设置回调数据。")
            return

        try:
            response = await self._controller.handle(
                f"/claude_mode {parts[2]}",
                _ctx(update),
            )
            await query.answer("已切换")
            if response.buttons:
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                keyboard = [
                    [InlineKeyboardButton(b["text"], callback_data=b["callback_data"])
                     for b in row]
                    for row in response.buttons
                ]
                await query.edit_message_text(
                    response.text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            else:
                await query.edit_message_text(response.text)
        except Exception as exc:
            logger.exception("Settings callback error")
            await query.answer(f"错误：{exc}")

    async def _conversation_callback_impl(
        self, update: Update, query: object, data: str
    ) -> None:
        from wlcodex.conversation_callback import decode_conversation_callback

        callback = decode_conversation_callback(data)
        if callback is None:
            await query.answer("无效的对话回调数据。")
            return

        try:
            response = await self._controller.handle_conversation_callback(callback)
            await query.answer("完成")
            if response.buttons:
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                keyboard = [
                    [InlineKeyboardButton(b["text"], callback_data=b["callback_data"])
                     for b in row]
                    for row in response.buttons
                ]
                await query.edit_message_text(
                    response.text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            else:
                await query.edit_message_text(response.text)
        except Exception as exc:
            logger.exception("Conversation callback error")
            await query.answer(f"错误：{exc}")

    async def _approval_callback_impl(
        self, update: Update, query: object, data: str
    ) -> None:
        from wlcodex.approval import decode_approval_callback

        callback = decode_approval_callback(data)
        if callback is None:
            await query.answer("无效的审批回调数据。")
            return

        try:
            msg = await self._approval.resolve_callback(
                callback, self._controller._backend, self._ledger
            )
            await query.answer("完成")
            await query.edit_message_text(
                f"{query.message.text}\n\n处理结果：{msg}"
                if query.message else msg
            )
        except Exception as exc:
            logger.exception("Approval callback error")
            await query.answer(f"错误：{exc}")

    async def _waiting_callback_impl(
        self, update: Update, query: object, data: str
    ) -> None:
        from wlcodex.waiting_callback import decode_waiting_callback

        callback = decode_waiting_callback(data)
        if callback is None:
            await query.answer("无效的等待回调数据。")
            return

        try:
            response = await self._controller.handle_waiting_callback(callback)
            await query.answer("完成")
            if response.buttons:
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                keyboard = [
                    [InlineKeyboardButton(b["text"], callback_data=b["callback_data"])
                     for b in row]
                    for row in response.buttons
                ]
                await query.edit_message_text(
                    response.text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            else:
                await query.edit_message_text(response.text)
        except Exception as exc:
            logger.exception("Waiting callback error")
            await query.answer(f"错误：{exc}")

    async def _worktree_done_callback_impl(
        self, update: Update, query: object, data: str
    ) -> None:
        from wlcodex.waiting_callback import decode_worktree_done_callback

        callback = decode_worktree_done_callback(data)
        if callback is None:
            await query.answer("无效的 worktree 回调数据。")
            return

        try:
            response = await self._controller.handle_worktree_done_callback(callback)
            await query.answer("完成")
            if response.buttons:
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                keyboard = [
                    [InlineKeyboardButton(b["text"], callback_data=b["callback_data"])
                     for b in row]
                    for row in response.buttons
                ]
                await query.edit_message_text(
                    response.text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            else:
                await query.edit_message_text(response.text)
        except Exception as exc:
            logger.exception("Worktree done callback error")
            await query.answer(f"错误：{exc}")

    # --- Legacy approval callback (kept for backward compat) ---

    async def approval_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.callback_router(update, context)

    async def _track_status_from_response(
        self, text: str, chat_id: int, message_id: int
    ) -> None:
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
) -> tuple[Application, WlCodexHandlers | None]:
    application = Application.builder().token(token).build()

    if controller is not None and ledger is not None:
        handlers = WlCodexHandlers(
            config, controller, ledger, approval_service, application.bot
        )
    else:
        application.add_handler(CommandHandler("start", _skeleton_start))
        return application, None

    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("help", handlers.help_cmd))
    application.add_handler(CommandHandler("task", handlers.task))
    application.add_handler(CommandHandler("tasks", handlers.tasks_list))
    application.add_handler(CommandHandler("status", handlers.status))
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
