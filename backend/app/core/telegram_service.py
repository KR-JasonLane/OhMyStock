"""Telegram durable outbox sender.

The service orchestration loop is added in a later task.  This module keeps the
delivery worker independently testable and never logs Telegram request bodies.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Protocol

from app.adapters.telegram import (TelegramAuthenticationError,
                                   TelegramPermanentError,
                                   TelegramRateLimited,
                                   TelegramTemporaryError)
from app.domain.notifications.formatting import delay_notice
from app.domain.notifications.models import NotificationPriority
from app.store.notification_store import Delivery, NotificationStore


class TelegramSenderPort(Protocol):
    async def send_message(self, chat_id: int, text: str) -> int: ...


class AuthenticationCircuit(Protocol):
    @property
    def is_dead(self) -> bool: ...

    def mark_dead(self, reason: str) -> None: ...


class TelegramCircuit:
    """Process-local circuit shared by poller and sender at composition time."""

    def __init__(self) -> None:
        self._reason: str | None = None

    @property
    def is_dead(self) -> bool:
        return self._reason is not None

    def mark_dead(self, reason: str) -> None:
        self._reason = reason

    def snapshot(self) -> dict[str, str | None]:
        return {"state": "dead" if self.is_dead else "running", "reason": self._reason}


class OutboxSender:
    """Claims fixed delivery parts and applies bounded retry policy.

    Every store operation is moved off the event loop.  The store's
    owner/version conditional updates make a late worker harmless after a
    lease has been reclaimed.
    """

    _BATCH_SIZE = 1
    _LEASE_S = 90
    _MAX_ATTEMPTS = 10
    _MAX_AGE = timedelta(hours=24)
    _DELAY_NOTICE_AFTER = timedelta(minutes=5)

    def __init__(
        self,
        store: NotificationStore,
        telegram: TelegramSenderPort,
        *,
        chat_id: int,
        worker_id: str,
        now: Callable[[], datetime] | None = None,
        random_float: Callable[[], float] | None = None,
        authentication_circuit: AuthenticationCircuit | None = None,
    ) -> None:
        if type(chat_id) is not int:
            raise ValueError("chat_id must be an int")
        self._store = store
        self._telegram = telegram
        self._chat_id = chat_id
        self._worker_id = worker_id
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._random_float = random_float or __import__("random").random
        self._authentication_circuit = authentication_circuit or TelegramCircuit()
        self._state = "running"
        self._last_run_claimed = 0

    async def run_once(self) -> int:
        if self._state == "dead" or self._authentication_circuit.is_dead:
            return 0
        deliveries = await asyncio.to_thread(
            self._store.claim_deliveries, self._worker_id, self._BATCH_SIZE, self._LEASE_S)
        self._last_run_claimed = len(deliveries)
        for delivery in deliveries:
            if self._is_expired(delivery):
                await asyncio.to_thread(
                    self._store.dead_letter_delivery,
                    delivery.id, self._worker_id, delivery.version,
                    "delivery_expired", None)
                continue
            if delivery.body is None:
                await asyncio.to_thread(
                    self._store.dead_letter_delivery,
                    delivery.id, self._worker_id, delivery.version,
                    "missing_body", None)
                continue

            text = self._display_text(delivery)
            try:
                message_id = await self._telegram.send_message(self._chat_id, text)
            except TelegramRateLimited as exc:
                await asyncio.to_thread(
                    self._store.retry_delivery,
                    delivery.id, self._worker_id, delivery.version,
                    exc.kind, 429, self._now() + timedelta(seconds=exc.retry_after))
            except TelegramAuthenticationError:
                # The Telegram client also opens its shared HTTP circuit.  Keep
                # the leased row untouched so a corrected configuration can
                # recover it after lease expiry; do not expose exception text.
                self._state = "dead"
                self._authentication_circuit.mark_dead("authentication_failed")
                break
            except TelegramPermanentError as exc:
                await asyncio.to_thread(
                    self._store.dead_letter_delivery,
                    delivery.id, self._worker_id, delivery.version,
                    exc.kind, _http_status(exc.kind))
                # A parent terminal transition invalidates sibling claims.
                break
            except TelegramTemporaryError as exc:
                await asyncio.to_thread(
                    self._store.retry_delivery,
                    delivery.id, self._worker_id, delivery.version,
                    exc.kind, _http_status(exc.kind),
                    self._now() + timedelta(seconds=self._retry_delay(delivery)))
            else:
                await asyncio.to_thread(
                    self._store.finish_delivery,
                    delivery.id, self._worker_id, delivery.version,
                    telegram_message_id=message_id)
        return len(deliveries)

    def snapshot(self) -> dict[str, int | str]:
        """Safe operational state; no body, chat ID, token, or error text."""
        state = "dead" if self._state == "dead" or self._authentication_circuit.is_dead else "running"
        return {"state": state, "last_run_claimed": self._last_run_claimed}

    def _is_expired(self, delivery: Delivery) -> bool:
        return (delivery.attempt_count >= self._MAX_ATTEMPTS
                or self._now() - delivery.created_at >= self._MAX_AGE)

    def _display_text(self, delivery: Delivery) -> str:
        assert delivery.body is not None
        if (delivery.priority == NotificationPriority.CRITICAL
                and self._now() - delivery.occurred_at >= self._DELAY_NOTICE_AFTER):
            return delay_notice(delivery.body, delivery.occurred_at)
        return delivery.body

    def _retry_delay(self, delivery: Delivery) -> float:
        # attempt_count is the number of previous failed sends.  A 25% bounded
        # positive jitter avoids synchronized recovery while preserving tests
        # with an injected zero random value.
        base = min(60.0, float(2 ** min(delivery.attempt_count, 6)))
        jitter = base * 0.25 * self._random_float()
        return base + jitter


def _http_status(kind: str) -> int | None:
    if kind.startswith("http_"):
        suffix = kind.removeprefix("http_")
        return int(suffix) if suffix.isdecimal() else None
    if kind == "rate_limited":
        return 429
    return None
