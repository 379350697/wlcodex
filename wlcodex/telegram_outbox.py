"""Telegram delivery outbox — isolates network failures from orchestration.

All send, edit, and callback-answer operations go through the outbox.
Delivery success/failure is recorded as runtime events; orchestration and
approval logic is never blocked by Telegram network problems.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from dataclasses import dataclass, field
from typing import Any

from wlcodex.runtime_events import (
    AggregateType,
    EventSource,
    EventType,
    RuntimeEvent,
    Visibility,
    now_iso,
    safe_text_preview,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------

MAX_RETRIES = 5
BASE_DELAY = 1.0  # seconds
MAX_DELAY = 60.0  # seconds
JITTER = 0.3  # ±30% of delay


def _backoff(attempt: int) -> float:
    """Exponential backoff with jitter. Attempt 1-based."""
    delay = min(BASE_DELAY * (2 ** (attempt - 1)), MAX_DELAY)
    jitter = random.uniform(-JITTER * delay, JITTER * delay)
    return delay + jitter


# ---------------------------------------------------------------------------
# Delivery request
# ---------------------------------------------------------------------------


@dataclass
class DeliveryRequest:
    """A queued Telegram delivery operation."""

    operation: str  # "send", "edit", "answer_callback"
    chat_id: int
    delivery_id: str  # stable idempotency key
    text: str = ""
    message_id: int = 0  # for edits
    buttons: list[list[dict[str, str]]] | None = None
    # The async function to call for delivery
    _send_fn: Any = field(default=None, repr=False)
    _edit_fn: Any = field(default=None, repr=False)
    _answer_fn: Any = field(default=None, repr=False)
    # Result
    result_message_id: int = -1
    attempts: int = 0
    last_error: str = ""
    last_error_type: str = ""


def make_delivery_id(operation: str, chat_id: int, text: str, message_id: int = 0) -> str:
    """Stable idempotency key for a delivery operation."""
    raw = f"{operation}:{chat_id}:{message_id}:{text[:200]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Outbox
# ---------------------------------------------------------------------------


class TelegramOutbox:
    """Queues Telegram delivery operations and processes them async.

    Delivery failures are isolated: they never fail the caller.
    Every delivery attempt is recorded as a runtime event.
    """

    def __init__(
        self,
        store: object | None = None,
        *,
        max_retries: int = MAX_RETRIES,
        sleep_fn: Any | None = None,
    ) -> None:
        self._store = store  # RuntimeEventStore
        self._max_retries = max_retries
        self._sleep = sleep_fn or asyncio.sleep
        self._queue: list[DeliveryRequest] = []
        self._pending: dict[str, DeliveryRequest] = {}
        self._processing = False
        self._waiters: dict[str, asyncio.Future[int]] = {}
        self._waitable_send_sequence = 0

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------

    def enqueue_send(
        self,
        chat_id: int,
        text: str,
        buttons: list[list[dict[str, str]]] | None = None,
        *,
        send_fn: Any = None,
        edit_fn: Any = None,
        correlation_id: str = "",
        delivery_id: str | None = None,
    ) -> str:
        """Enqueue a send operation. Returns the delivery_id."""
        delivery_id = delivery_id or make_delivery_id("send", chat_id, text)
        req = DeliveryRequest(
            operation="send",
            chat_id=chat_id,
            delivery_id=delivery_id,
            text=text,
            buttons=buttons,
            _send_fn=send_fn,
            _edit_fn=edit_fn,
        )
        self._queue.append(req)
        self._emit_enqueued(req, correlation_id)
        return delivery_id

    def enqueue_edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        buttons: list[list[dict[str, str]]] | None = None,
        *,
        edit_fn: Any = None,
        correlation_id: str = "",
    ) -> str:
        """Enqueue an edit operation. Returns the delivery_id."""
        delivery_id = make_delivery_id("edit", chat_id, text, message_id)
        req = DeliveryRequest(
            operation="edit",
            chat_id=chat_id,
            delivery_id=delivery_id,
            text=text,
            message_id=message_id,
            buttons=buttons,
            _edit_fn=edit_fn,
        )
        self._queue.append(req)
        self._emit_enqueued(req, correlation_id)
        return delivery_id

    def enqueue_answer_callback(
        self,
        text: str,
        *,
        answer_fn: Any = None,
        correlation_id: str = "",
    ) -> str:
        """Enqueue a callback answer. Returns the delivery_id.

        Callback answer failures are recorded as events only.
        """
        delivery_id = make_delivery_id("answer_callback", 0, text)
        req = DeliveryRequest(
            operation="answer_callback",
            chat_id=0,
            delivery_id=delivery_id,
            text=text,
            _answer_fn=answer_fn,
        )
        self._queue.append(req)
        self._emit_enqueued(req, correlation_id)
        return delivery_id

    async def enqueue_send_wait(
        self,
        chat_id: int,
        text: str,
        buttons: list[list[dict[str, str]]] | None = None,
        *,
        send_fn: Any = None,
        edit_fn: Any = None,
        correlation_id: str = "",
        timeout_seconds: float = 5.0,
    ) -> int:
        self._waitable_send_sequence += 1
        delivery_id = (
            f"{make_delivery_id('send', chat_id, text)}"
            f"-wait-{self._waitable_send_sequence}"
        )
        self.enqueue_send(
            chat_id,
            text,
            buttons,
            send_fn=send_fn,
            edit_fn=edit_fn,
            correlation_id=correlation_id,
            delivery_id=delivery_id,
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[int] = loop.create_future()
        self._waiters[delivery_id] = future
        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except TimeoutError:
            return -1
        finally:
            self._waiters.pop(delivery_id, None)

    # ------------------------------------------------------------------
    # Process (async)
    # ------------------------------------------------------------------

    async def process_all(self) -> None:
        """Process all queued deliveries. Call from an async loop."""
        if self._processing:
            return
        self._processing = True
        try:
            while self._queue:
                req = self._queue.pop(0)
                await self._deliver(req)
        finally:
            self._processing = False

    async def _deliver(self, req: DeliveryRequest) -> None:
        """Attempt delivery with retry."""
        for attempt in range(1, self._max_retries + 1):
            req.attempts = attempt
            try:
                self._emit_event(
                    EventType.TELEGRAM_DELIVERY_STARTED,
                    payload={
                        "delivery_id": req.delivery_id,
                        "operation": req.operation,
                        "attempt": attempt,
                        "chat_id": req.chat_id,
                    },
                )
                await self._execute(req)
                # Success
                if req.operation == "send":
                    self._emit_event(EventType.TELEGRAM_MESSAGE_SENT, payload={
                        "delivery_id": req.delivery_id,
                        "message_id": req.result_message_id,
                        "chat_id": req.chat_id,
                        "operation": req.operation,
                        "attempt": attempt,
                    })
                    waiter = self._waiters.get(req.delivery_id)
                    if waiter is not None and not waiter.done():
                        waiter.set_result(req.result_message_id)
                elif req.operation == "edit":
                    self._emit_event(EventType.TELEGRAM_MESSAGE_EDITED, payload={
                        "delivery_id": req.delivery_id,
                        "message_id": req.message_id,
                        "chat_id": req.chat_id,
                        "operation": req.operation,
                        "attempt": attempt,
                    })
                return
            except Exception as exc:
                req.last_error = str(exc)
                req.last_error_type = type(exc).__name__
                is_retryable = _is_retryable(exc)
                is_not_modified = "message is not modified" in str(exc).lower()

                if is_not_modified:
                    self._emit_event(EventType.TELEGRAM_EDIT_SKIPPED_NO_CHANGE, payload={
                        "delivery_id": req.delivery_id,
                        "message_id": req.message_id,
                    })
                    return  # not a real failure

                if attempt < self._max_retries and is_retryable:
                    delay = _backoff(attempt)
                    self._emit_event(EventType.TELEGRAM_MESSAGE_FAILED, payload={
                        "delivery_id": req.delivery_id,
                        "operation": req.operation,
                        "attempt": attempt,
                        "error_type": req.last_error_type,
                        "retryable": True,
                    })
                    self._emit_event(EventType.TELEGRAM_OUTBOX_RETRY_SCHEDULED, payload={
                        "delivery_id": req.delivery_id,
                        "next_attempt": attempt + 1,
                        "delay_seconds": round(delay, 2),
                    })
                    await self._sleep(delay)
                else:
                    self._emit_event(EventType.TELEGRAM_MESSAGE_FAILED, payload={
                        "delivery_id": req.delivery_id,
                        "operation": req.operation,
                        "attempt": attempt,
                        "error_type": req.last_error_type,
                        "retryable": False,
                    })
                    if attempt >= self._max_retries:
                        self._emit_event(EventType.TELEGRAM_OUTBOX_GAVE_UP, payload={
                            "delivery_id": req.delivery_id,
                            "attempts": attempt,
                        })
                    waiter = self._waiters.get(req.delivery_id)
                    if waiter is not None and not waiter.done():
                        waiter.set_result(-1)
                    return

    async def _execute(self, req: DeliveryRequest) -> None:
        if req.operation == "send" and req._send_fn is not None:
            req.result_message_id = await req._send_fn(
                req.chat_id, req.text, req.buttons
            )
        elif req.operation == "edit" and req._edit_fn is not None:
            await req._edit_fn(
                req.chat_id, req.message_id, req.text, req.buttons
            )
        elif req.operation == "answer_callback" and req._answer_fn is not None:
            await req._answer_fn(req.text)

    # ------------------------------------------------------------------
    # Event helpers
    # ------------------------------------------------------------------

    def _emit_enqueued(self, req: DeliveryRequest, correlation_id: str) -> None:
        self._emit_event(
            EventType.TELEGRAM_DELIVERY_ENQUEUED,
            payload={
                "delivery_id": req.delivery_id,
                "operation": req.operation,
                "chat_id": req.chat_id,
                "message_id": req.message_id,
                "text_preview": safe_text_preview(req.text),
                "text_length": len(req.text),
            },
            correlation_id=correlation_id,
        )

    def _emit_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        correlation_id: str = "",
    ) -> None:
        if self._store is None:
            return
        try:
            self._store.append(RuntimeEvent(
                schema_version=1,
                event_type=event_type,
                aggregate_type=AggregateType.TELEGRAM_MESSAGE,
                aggregate_id=payload.get("delivery_id", "unknown"),
                correlation_id=correlation_id,
                source=EventSource.TELEGRAM,
                actor="telegram_outbox",
                visibility=Visibility.INTERNAL,
                payload=payload,
                occurred_at=now_iso(),
            ))
        except Exception:
            logger.debug("Failed to emit outbox event", exc_info=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_retryable(exc: Exception) -> bool:
    """Return True if the exception is a transient network error."""
    exc_name = type(exc).__name__
    if exc_name in ("TimedOut", "NetworkError", "ConnectionError"):
        return True
    msg = str(exc).lower()
    retryable_keywords = (
        "timed out", "timeout", "connection reset", "connection refused",
        "network", "too many requests", "retry after", "server error",
        "503", "502", "504",
    )
    return any(kw in msg for kw in retryable_keywords)
