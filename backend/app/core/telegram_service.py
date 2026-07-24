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
from app.domain.notifications.digest import DigestBuilder, DigestPlanner
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


class Maintenance:
    """Telegram 보존 정리의 비동기 경계.

    Lifespan service가 매일 03:30 KST에 이 메서드를 호출한다. 동기 SQLAlchemy
    저장소는 event loop를 점유하지 않도록 항상 thread로 보낸다.
    """

    def __init__(self, store: NotificationStore, *,
                 now: Callable[[], datetime] | None = None,
                 batch_size: int = 1000) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._store = store
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._batch_size = batch_size

    async def run_once(self) -> int:
        return await asyncio.to_thread(
            self._store.maintenance_cleanup, self._now(), self._batch_size)


class DigestService:
    """Planner가 고른 거래일을 durable digest outbox로 materialize한다."""

    def __init__(self, planner: DigestPlanner, builder: DigestBuilder,
                 store: NotificationStore, *,
                 now: Callable[[], datetime] | None = None) -> None:
        self._planner = planner
        self._builder = builder
        self._store = store
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def run_once(self) -> int:
        now = self._now()
        due_dates = await asyncio.to_thread(self._planner.due_dates, now)
        created = 0
        for trading_day in due_dates:
            digest = await self._builder.build(trading_day)
            result = await asyncio.to_thread(
                self._store.materialize_digest,
                digest.idempotency_key, digest.payload, digest.bodies,
                occurred_at=now)
            if result.created:
                created += 1
                self._planner.mark_generated(trading_day)
        return created


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
    _TTL_SCRUB_BATCH = 10
    # Adapter httpx timeout is phase-based.  This application deadline is the
    # total external exposure bound; store claim keeps a 5s margin (35s).
    _SEND_DEADLINE_S = 30

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
        if self._store.SENSITIVE_DELIVERY_MIN_TTL_S <= self._SEND_DEADLINE_S:
            raise ValueError("sensitive delivery TTL guard must exceed send deadline")
        self._state = "running"
        self._last_run_claimed = 0

    async def run_once(self) -> int:
        if self._state == "dead" or self._authentication_circuit.is_dead:
            # Dead sender도 작은 bounded chunk를 계속 scrub한다. 대량 backlog는
            # Task 8 maintenance가 맡고, delivery 우선순위와 경합하지 않는다.
            await asyncio.to_thread(
                self._store.purge_expired_sensitive, self._now(), self._TTL_SCRUB_BATCH)
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
                message_id = await asyncio.wait_for(
                    self._telegram.send_message(self._chat_id, text),
                    timeout=self._SEND_DEADLINE_S)
            except TimeoutError:
                await asyncio.to_thread(
                    self._store.retry_delivery,
                    delivery.id, self._worker_id, delivery.version,
                    "send_deadline", None,
                    self._now() + timedelta(seconds=self._retry_delay(delivery)))
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
        # CRITICAL delivery의 claim/send가 먼저다. 정상 sender tick은 만료
        # 본문을 작은 후순위 chunk로 scrub해 daily maintenance 사이 노출도 줄인다.
        await asyncio.to_thread(
            self._store.purge_expired_sensitive, self._now(), self._TTL_SCRUB_BATCH)
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
