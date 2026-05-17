from __future__ import annotations

import logging
from typing import Any

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from wlcodex.config import AppConfig
from wlcodex.controller import CommandController
from wlcodex.db import Ledger

logger = logging.getLogger(__name__)


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

    # --- Telegram send/edit callbacks for event bridge ---

    async def send_telegram(
        self, chat_id: int, text: str, buttons: list[list[dict[str, str]]] | None = None
    ) -> int:
        """Send a message via the bot. Returns message_id."""
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
        """Edit an existing message, optionally with inline keyboard buttons."""
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
                logger.debug("Telegram message %d already has the requested text", message_id)
                return
            logger.debug("Failed to edit message %d, sending new one", message_id)
            new_msg = await self._bot.send_message(
                chat_id=chat_id, text=text, reply_markup=reply_markup,
            )
            # Update status message mapping if the old one was a task card
            for task in self._ledger.list_tasks(limit=50, include_archived=True):
                if task.telegram_status_message_id == message_id:
                    self._ledger.set_status_message(task.id, chat_id, new_msg.message_id)

    # --- Handlers ---

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        from wlcodex.status import render_help
        await update.effective_message.reply_text(render_help())

    async def help_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._guard(update):
            return
        from wlcodex.status import render_help
        await update.effective_message.reply_text(render_help())

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

    # --- Callback routing ---

    async def callback_router(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Route callbacks by prefix: approval:*, waiting:*, worktree_done:*."""
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
        else:
            await query.answer("未知回调类型。")

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


def _is_message_not_modified_error(exc: Exception) -> bool:
    return "message is not modified" in str(exc).lower()
