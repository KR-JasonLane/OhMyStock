"""Telegram polling, command lanes, projection, sending and lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Collection
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from pydantic import SecretStr

from app.adapters.telegram import (TelegramAuthenticationError,
                                   TelegramPermanentError,
                                   TelegramRateLimited,
                                   TelegramTemporaryError)
from app.domain.notifications.formatting import delay_notice, render_parts
from app.domain.notifications.digest import DigestBuilder, DigestPlanner
from app.domain.notifications.authorization import is_authorized
from app.domain.notifications.models import (
    CommandKind,
    InvalidCommand,
    NotificationPriority,
    OperatorIdentity,
)
from app.domain.notifications.parsing import parse_command
from app.store.notification_store import Delivery, NotificationStore

logger = logging.getLogger(__name__)


def external_id_hash(
        bot_token: SecretStr | str, kind: str, external_id: int | str) -> str:
    """Versioned keyed hash; neither the token nor source identifier is retained."""
    if not kind or ":" in kind:
        raise ValueError("kind must be a non-empty label without ':'")
    secret = (bot_token.get_secret_value()
              if isinstance(bot_token, SecretStr) else bot_token)
    if not secret:
        raise ValueError("bot token must be non-empty")
    message = f"v1:{kind}:{external_id}".encode()
    return "v1:" + hmac.new(
        secret.encode(), message, hashlib.sha256).hexdigest()


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


class InboxPoller:
    """One leased Bot API poll whose DB effects commit in one transaction."""

    _ALLOWED_BACKLOG_LIMIT = 1000
    _LEASE_S = 40
    _REJECTED_CARDINALITY = 300
    _QUERY_CAPACITY = 20
    _QUERY = frozenset({
        CommandKind.STATUS.value, CommandKind.ACCOUNT.value,
        CommandKind.POSITIONS.value, CommandKind.HELP.value,
    })

    def __init__(
        self, *, telegram, inbox, operators: Collection[OperatorIdentity],
        bot_token: SecretStr, worker_id: str, circuit: TelegramCircuit,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._telegram = telegram
        self._inbox = inbox
        self._operators = tuple(operators)
        self._bot_token = bot_token
        self._worker_id = worker_id
        self._circuit = circuit
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._last_update_id: int | None = None
        self._last_success_at: datetime | None = None
        self._backoff_reason: str | None = None
        self._commit_task: asyncio.Task | None = None

    async def run_once(self) -> int:
        if self._circuit.is_dead:
            return 0
        if not await asyncio.to_thread(
                self._inbox.can_poll, self._ALLOWED_BACKLOG_LIMIT):
            self._backoff_reason = "allowed_queue_full"
            return 0
        generation = await asyncio.to_thread(
            self._inbox.acquire_poller_lease, self._worker_id, self._LEASE_S)
        if generation is None:
            self._backoff_reason = "poller_lease_held"
            return 0
        try:
            offset = await asyncio.to_thread(self._inbox.current_offset)
            try:
                messages = await self._telegram.get_updates(offset)
            except TelegramAuthenticationError:
                self._circuit.mark_dead("authentication_failed")
                return 0
            accepted: list[dict[str, object]] = []
            rejected: list[str] = []
            next_offset = offset
            received_at = self._now()
            if received_at.tzinfo is None or received_at.utcoffset() is None:
                raise ValueError("now must be timezone-aware")
            query_depth = await asyncio.to_thread(
                self._inbox.pending_count, self._QUERY)
            for message in sorted(messages, key=lambda item: item.update_id):
                next_offset = max(next_offset, message.update_id + 1)
                subject = external_id_hash(
                    self._bot_token, "subject",
                    f"{message.user_id}:{message.chat_id}")
                if not is_authorized(message, self._operators):
                    rejected.append(subject)
                    continue
                try:
                    parsed = parse_command(message.text)
                except InvalidCommand:
                    rejected.append(subject)
                    continue
                if parsed.kind.value in self._QUERY:
                    if query_depth >= self._QUERY_CAPACITY:
                        rejected.append(subject)
                        self._backoff_reason = "query_queue_full"
                        continue
                    query_depth += 1
                accepted.append({
                    "update_id": message.update_id,
                    "operator_hash": external_id_hash(
                        self._bot_token, "user", message.user_id),
                    "command": parsed.kind.value,
                    "argument_hash": parsed.argument_hash,
                    "correlation_id": f"telegram-{message.update_id}",
                    "received_at": received_at,
                })
            minute = received_at.replace(second=0, microsecond=0)
            def commit_and_release() -> bool:
                try:
                    return self._inbox.persist_leased_poll_batch(
                        self._worker_id, generation, accepted, rejected,
                        next_offset=next_offset, minute=minute,
                        cardinality_limit=self._REJECTED_CARDINALITY)
                finally:
                    self._inbox.release_poller_lease(
                        self._worker_id, generation)

            commit_task = asyncio.create_task(asyncio.to_thread(
                commit_and_release))
            self._commit_task = commit_task
            try:
                committed = await asyncio.shield(commit_task)
            finally:
                if commit_task.done():
                    self._commit_task = None
            if not committed:
                self._backoff_reason = "poller_lease_lost"
                return 0
            if messages:
                self._last_update_id = max(item.update_id for item in messages)
            self._last_success_at = received_at
            if self._backoff_reason != "query_queue_full":
                self._backoff_reason = None
            return len(messages)
        finally:
            # A cancelled wrapper must not release the lease ahead of its
            # still-running DB commit. The generation expires if the bounded
            # shutdown join cannot recover it.
            if self._commit_task is None or self._commit_task.done():
                await asyncio.to_thread(
                    self._inbox.release_poller_lease,
                    self._worker_id, generation)

    async def finish_commit(self) -> None:
        """Join a DB commit that survived cancellation of its poller wrapper."""
        task = self._commit_task
        if task is not None:
            try:
                await asyncio.shield(task)
            except Exception as exc:
                logger.error(
                    "telegram inbox commit failed during shutdown kind=%s",
                    _error_kind(exc))
            finally:
                self._commit_task = None

    def snapshot(self) -> dict[str, object]:
        return {
            "dead": self._circuit.is_dead,
            "last_update_id": self._last_update_id,
            "last_success_at": (
                self._last_success_at.isoformat()
                if self._last_success_at is not None else None),
            "backoff_reason": self._backoff_reason,
        }


class CommandDispatcher:
    """Ordered control lane plus one bounded, single-worker query lane."""

    _QUERY = frozenset({
        CommandKind.STATUS.value, CommandKind.ACCOUNT.value,
        CommandKind.POSITIONS.value, CommandKind.HELP.value,
    })
    _CONTROL = frozenset({
        CommandKind.PAUSE.value, CommandKind.STOP.value,
        CommandKind.RESUME.value, CommandKind.LIQUIDATE_ALL.value,
        CommandKind.CONFIRM.value,
    })

    def __init__(
        self, inbox, processor, *, worker_id: str,
        now: Callable[[], datetime] | None = None, query_capacity: int = 20,
        response_publisher=None,
    ) -> None:
        if query_capacity < 1:
            raise ValueError("query_capacity must be positive")
        self._inbox = inbox
        self._processor = processor
        self._worker_id = worker_id
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._query_capacity = query_capacity
        self._response_publisher = response_publisher
        self._query_queue_depth = 0
        self._accepting = True
        self._last_control_age_s = 0.0
        self._control_delay_warning = False

    async def _claim(self, commands: Collection[str]):
        if not self._accepting:
            return None
        return await asyncio.to_thread(
            self._inbox.claim_next, self._worker_id, 15, commands=commands)

    async def _finish_claimed(self, claimed) -> Any:
        try:
            result = await self._processor.process_claimed(claimed)
        except BaseException:
            await asyncio.to_thread(
                self._inbox.release, claimed.update_id,
                self._worker_id, claimed.version)
            raise
        if result.kind == "deferred":
            await asyncio.to_thread(
                self._inbox.release, claimed.update_id,
                self._worker_id, claimed.version)
        else:
            if self._response_publisher is not None:
                try:
                    await self._response_publisher.publish(claimed, result)
                except BaseException:
                    await asyncio.to_thread(
                        self._inbox.release, claimed.update_id,
                        self._worker_id, claimed.version)
                    raise
            await asyncio.to_thread(
                self._inbox.finish, claimed.update_id,
                self._worker_id, claimed.version)
        return result

    async def _refresh_query_depth(self) -> None:
        self._query_queue_depth = await asyncio.to_thread(
            self._inbox.pending_count, self._QUERY,
            limit=self._query_capacity)

    async def tick_control(self) -> int:
        await self._refresh_query_depth()
        processed = 0
        while self._accepting:
            claimed = await self._claim(self._CONTROL)
            if claimed is None:
                break
            age = max(0.0, (self._now() - claimed.received_at).total_seconds())
            self._last_control_age_s = age
            self._control_delay_warning = age > 5.0
            result = await self._finish_claimed(claimed)
            if result.kind == "deferred":
                break
            processed += 1
        return processed

    async def tick_query(self) -> int:
        claimed = await self._claim(self._QUERY)
        if claimed is None:
            await self._refresh_query_depth()
            return 0
        await self._finish_claimed(claimed)
        await self._refresh_query_depth()
        return 1

    async def reconcile_unknown(self) -> int:
        result = await self._processor.reconcile_unknown()
        return int(result is not None)

    async def begin_shutdown(self) -> None:
        self._accepting = False

    async def finish_shutdown(self) -> None:
        await self._processor.shutdown_accepted_monitors()

    def snapshot(self) -> dict[str, object]:
        return {
            "query_queue_depth": self._query_queue_depth,
            "query_queue_capacity": self._query_capacity,
            "oldest_control_age_s": self._last_control_age_s,
            "control_delay_warning": self._control_delay_warning,
        }


class EphemeralResponseSender:
    """In-memory confirmation delivery; raw tokens never cross a storage boundary."""

    _CAPACITY = 20
    _SEND_DEADLINE_S = 30

    def __init__(
            self, telegram: TelegramSenderPort, *, chat_id: int,
            circuit: TelegramCircuit,
            now: Callable[[], datetime] | None = None) -> None:
        if type(chat_id) is not int:
            raise ValueError("chat_id must be an int")
        self._telegram = telegram
        self._chat_id = chat_id
        self._circuit = circuit
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._queue: deque[tuple[str, datetime]] = deque()
        self._retry_at: datetime | None = None
        self._failures = 0

    async def enqueue(
            self, text: str, *,
            expires_at: datetime | None = None) -> None:
        if not text or len(text) > 4096:
            raise ValueError("ephemeral response must be 1..4096 characters")
        if len(self._queue) >= self._CAPACITY:
            raise RuntimeError("ephemeral response queue full")
        expiry = expires_at or self._now() + timedelta(seconds=120)
        if expiry.tzinfo is None or expiry.utcoffset() is None:
            raise ValueError("ephemeral response expiry must be timezone-aware")
        if expiry <= self._now():
            return
        self._queue.append((text, expiry))

    async def run_once(self) -> int:
        now = self._now()
        while self._queue and self._queue[0][1] <= now:
            self._queue.popleft()
        if self._circuit.is_dead or not self._queue:
            return 0
        if not self._queue:
            self._retry_at = None
            return 0
        if self._retry_at is not None and now < self._retry_at:
            return 0
        text, expires_at = self._queue[0]
        remaining_s = (expires_at - now).total_seconds()
        try:
            await asyncio.wait_for(
                self._telegram.send_message(self._chat_id, text),
                timeout=min(self._SEND_DEADLINE_S, remaining_s))
        except TelegramRateLimited as exc:
            self._retry_at = now + timedelta(seconds=exc.retry_after)
        except TelegramAuthenticationError:
            self._circuit.mark_dead("authentication_failed")
        except TelegramTemporaryError:
            self._failures += 1
            self._retry_at = now + timedelta(
                seconds=min(60, 2 ** min(self._failures, 6)))
        except (TelegramPermanentError, TimeoutError):
            # A raw token cannot be placed in a durable retry store. Permanent
            # failure is terminal; the operator can safely request a new token.
            self._queue.popleft()
            self._retry_at = None
            self._failures = 0
        else:
            self._queue.popleft()
            self._retry_at = None
            self._failures = 0
        return 1

    def clear(self) -> None:
        self._queue.clear()
        self._retry_at = None

    def snapshot(self) -> dict[str, object]:
        return {
            "state": "dead" if self._circuit.is_dead else "running",
            "pending": len(self._queue),
        }


class CommandResponsePublisher:
    """Publish normal responses durably and confirmation secrets in memory only."""

    def __init__(
            self, store: NotificationStore,
            ephemeral_sender: EphemeralResponseSender) -> None:
        self._store = store
        self._ephemeral_sender = ephemeral_sender

    async def publish(self, claimed, result) -> None:
        text = result.response_text
        if text is None:
            return
        if result.ephemeral:
            await self._ephemeral_sender.enqueue(
                text, expires_at=result.confirmation_expires_at)
            return
        parts = render_parts(text, claimed.correlation_id)
        await asyncio.to_thread(
            self._store.enqueue_parts,
            f"command-response-{claimed.update_id}",
            tuple(part.text for part in parts),
            sensitive=result.outbox_sensitive,
            payload={
                "version": 1,
                "command": claimed.command,
                "result_kind": result.kind,
            },
            retention_kind="query" if result.outbox_sensitive else "standard",
        )


class CompositeSender:
    """Prioritize volatile confirmations, then drain the durable outbox."""

    def __init__(self, ephemeral: EphemeralResponseSender, outbox) -> None:
        self._ephemeral = ephemeral
        self._outbox = outbox

    async def run_once(self) -> int:
        sent = await self._ephemeral.run_once()
        return sent if sent else await self._outbox.run_once()

    async def release_leases(self) -> int:
        self._ephemeral.clear()
        result = await self._outbox.release_leases()
        return int(result or 0)

    def clear_ephemeral(self) -> None:
        self._ephemeral.clear()

    def snapshot(self) -> dict[str, object]:
        return {
            "ephemeral": self._ephemeral.snapshot(),
            "outbox": self._outbox.snapshot(),
        }


class AsyncProjector:
    def __init__(self, projector) -> None:
        self._projector = projector
        self._checkpoint: int | None = None

    async def run_once(self) -> int:
        def project() -> tuple[int, int]:
            projected = self._projector.project_batch()
            return projected, self._projector.checkpoint()

        projected, self._checkpoint = await asyncio.to_thread(project)
        return projected

    def snapshot(self) -> dict[str, object]:
        return {"checkpoint": self._checkpoint}


class TelegramMaintenance:
    """Frequent idempotent digest check and bounded once-per-day cleanup."""

    def __init__(
        self, digest: "DigestService", maintenance: "Maintenance", *,
        now: Callable[[], datetime] | None = None,
        scheduler_snapshot: Callable[[], dict[str, object] | None] | None = None,
    ) -> None:
        self._digest = digest
        self._maintenance = maintenance
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._scheduler_snapshot = scheduler_snapshot or (lambda: None)
        self._last_cleanup_day = None
        self._scheduler_dead = False
        self._scheduler_dead_transitions = 0

    async def run_once(self) -> int:
        created = await self._digest.run_once()
        now = self._now()
        kst = now.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Seoul"))
        if ((kst.hour, kst.minute) >= (3, 30)
                and self._last_cleanup_day != kst.date()):
            await self._maintenance.run_once()
            self._last_cleanup_day = kst.date()
        snapshot = self._scheduler_snapshot()
        dead = bool(snapshot and snapshot.get("dead") is True)
        if dead and not self._scheduler_dead:
            self._scheduler_dead_transitions += 1
        self._scheduler_dead = dead
        return created

    def snapshot(self) -> dict[str, object]:
        return {
            "scheduler_dead": self._scheduler_dead,
            "scheduler_dead_transitions": self._scheduler_dead_transitions,
        }


class TelegramService:
    """Own only Telegram child loops; main remains the runtime shutdown owner."""

    def __init__(
        self, *, poller, dispatcher, projector, sender, maintenance,
        circuit: TelegramCircuit | None = None, failure_budget: int = 3,
        idle_s: float = 0.25,
    ) -> None:
        if failure_budget < 1 or idle_s < 0:
            raise ValueError("invalid Telegram loop supervision settings")
        self._poller = poller
        self._dispatcher = dispatcher
        self._projector = projector
        self._sender = sender
        self._maintenance = maintenance
        self._circuit = circuit or TelegramCircuit()
        self._failure_budget = failure_budget
        self._idle_s = idle_s
        self._loop_idle_s = {
            "commands": min(idle_s, 0.1),
            "queries": min(idle_s, 0.1),
            "maintenance": max(idle_s, 30.0),
        }
        self._tasks: dict[str, asyncio.Task] = {}
        self._states: dict[str, dict[str, object]] = {}
        self._shutdown_started = False
        self._shutdown_spent_s = 0.0
        self.stop_trace: list[str] = []

    def start(self) -> None:
        if self._tasks:
            return
        operations: dict[str, Callable[[], Awaitable[Any]]] = {
            "poller": self._poller.run_once,
            "projector": self._projector.run_once,
            "sender": self._sender.run_once,
            "maintenance": self._maintenance.run_once,
        }
        if hasattr(self._dispatcher, "tick_control"):
            operations["commands"] = self._control_once
            operations["queries"] = self._dispatcher.tick_query
            operations["reconciliation"] = self._dispatcher.reconcile_unknown
        else:
            operations["commands"] = self._dispatcher.run_once
        for name, operation in operations.items():
            self._states[name] = {
                "dead": False, "failures": 0, "last_error_kind": None}
            self._tasks[name] = asyncio.create_task(
                self._supervise(name, operation), name=f"telegram-{name}")

    async def _control_once(self) -> int:
        return await self._dispatcher.tick_control()

    async def _supervise(
            self, name: str, operation: Callable[[], Awaitable[Any]]) -> None:
        consecutive = 0
        while True:
            try:
                await operation()
            except asyncio.CancelledError:
                raise
            except TelegramRateLimited as exc:
                state = self._states[name]
                state["failures"] = 0
                state["last_error_kind"] = "rate_limited"
                logger.warning(
                    "telegram loop backoff loop=%s kind=rate_limited seconds=%d",
                    name, exc.retry_after)
                await asyncio.sleep(exc.retry_after)
                continue
            except TelegramTemporaryError:
                consecutive = 0
                state = self._states[name]
                transient = int(state.get("transient_failures", 0)) + 1
                state["transient_failures"] = transient
                state["failures"] = 0
                state["last_error_kind"] = "temporary_error"
                delay = min(60.0, float(2 ** min(transient, 6)))
                logger.warning(
                    "telegram loop backoff loop=%s kind=temporary_error seconds=%s",
                    name, delay)
                await asyncio.sleep(delay)
                continue
            except Exception as exc:  # isolated child budget
                consecutive += 1
                state = self._states[name]
                state["failures"] = consecutive
                state["last_error_kind"] = _error_kind(exc)
                logger.error(
                    "telegram loop failure loop=%s kind=%s attempt=%d",
                    name, state["last_error_kind"], consecutive)
                if consecutive >= self._failure_budget:
                    state["dead"] = True
                    return
            else:
                consecutive = 0
                self._states[name]["failures"] = 0
                self._states[name]["transient_failures"] = 0
            await asyncio.sleep(self._loop_idle_s.get(name, self._idle_s))

    async def begin_shutdown(self) -> None:
        if self._shutdown_started:
            return
        loop = asyncio.get_running_loop()
        began_at = loop.time()
        self._shutdown_started = True
        poller = self._tasks.get("poller")
        if poller is not None:
            poller.cancel()
        self.stop_trace.append("poller")
        if poller is not None:
            try:
                await asyncio.wait_for(
                    asyncio.gather(poller, return_exceptions=True), timeout=2)
            except TimeoutError:
                pass
        remaining_commit_s = max(0.0, began_at + 2 - loop.time())
        if remaining_commit_s and hasattr(self._poller, "finish_commit"):
            try:
                await asyncio.wait_for(
                    self._poller.finish_commit(), timeout=remaining_commit_s)
            except TimeoutError:
                logger.error("telegram inbox commit exceeded deadline")
        self.stop_trace.append("inbox_commit")
        if hasattr(self._dispatcher, "begin_shutdown"):
            await self._dispatcher.begin_shutdown()
        for name in ("commands", "queries", "reconciliation"):
            task = self._tasks.get(name)
            if task is not None:
                task.cancel()
        command_tasks = [
            self._tasks[name] for name in ("commands", "queries", "reconciliation")
            if name in self._tasks]
        if command_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*command_tasks, return_exceptions=True),
                    timeout=2)
            except TimeoutError:
                logger.error("telegram command shutdown exceeded deadline")
        self.stop_trace.append("command_claims")
        self._shutdown_spent_s += loop.time() - began_at

    async def finish_shutdown(self, deadline_s: float = 10) -> None:
        if deadline_s <= 0:
            raise ValueError("deadline_s must be positive")

        async def finish() -> None:
            if hasattr(self._dispatcher, "finish_shutdown"):
                await self._dispatcher.finish_shutdown()
            await self._stop_task("projector", final_run=self._projector.run_once)
            self.stop_trace.append("projector")
            await self._stop_task("sender")
            for _ in range(100):
                if await self._sender.run_once() == 0:
                    break
            self.stop_trace.append("sender")
            await self._stop_task("maintenance")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, deadline_s - self._shutdown_spent_s)
        timed_out = False

        async def within(awaitable) -> bool:
            nonlocal timed_out
            remaining = deadline - loop.time()
            if remaining <= 0:
                if hasattr(awaitable, "close"):
                    awaitable.close()
                timed_out = True
                return False
            try:
                await asyncio.wait_for(awaitable, timeout=remaining)
                return True
            except TimeoutError:
                timed_out = True
                return False

        await within(finish())
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        await within(asyncio.gather(*self._tasks.values(), return_exceptions=True))
        if hasattr(self._sender, "clear_ephemeral"):
            self._sender.clear_ephemeral()
        if hasattr(self._sender, "release_leases"):
            await within(self._sender.release_leases())
        if timed_out:
            logger.error("telegram shutdown deadline exceeded")

    async def _stop_task(
            self, name: str,
            final_run: Callable[[], Awaitable[Any]] | None = None) -> None:
        task = self._tasks.get(name)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if final_run is not None:
            await final_run()

    def snapshot(self) -> dict[str, object]:
        result = {
            name: dict(state) for name, state in self._states.items()
        }
        if "poller" in result and self._circuit.is_dead:
            result["poller"]["dead"] = True
        if "sender" in result and self._circuit.is_dead:
            result["sender"]["dead"] = True
        result["enabled"] = True
        result["dead"] = all(
            bool(state.get("dead")) for state in result.values()
            if isinstance(state, dict))
        result["last_error_kind"] = self._circuit.snapshot()["reason"]
        if hasattr(self._dispatcher, "snapshot"):
            result["dispatcher"] = self._dispatcher.snapshot()
        for name, component in (
                ("poller", self._poller),
                ("projector", self._projector),
                ("sender", self._sender),
                ("maintenance", self._maintenance)):
            if name in result and hasattr(component, "snapshot"):
                supervisor_dead = bool(result[name].get("dead"))
                result[name].update(component.snapshot())
                if supervisor_dead:
                    result[name]["dead"] = True
        return result


def _error_kind(exc: Exception) -> str:
    if isinstance(exc, TelegramAuthenticationError):
        return "authentication_failed"
    if isinstance(exc, TelegramRateLimited):
        return "rate_limited"
    if isinstance(exc, TelegramTemporaryError):
        return "temporary_error"
    if isinstance(exc, TelegramPermanentError):
        return "permanent_error"
    return "internal_error"


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
        self._delivery_counts = {
            "pending": 0, "sending": 0, "sent": 0, "dead_letter": 0}

    async def run_once(self) -> int:
        if self._state == "dead" or self._authentication_circuit.is_dead:
            # Dead sender도 작은 bounded chunk를 계속 scrub한다. 대량 backlog는
            # Task 8 maintenance가 맡고, delivery 우선순위와 경합하지 않는다.
            await asyncio.to_thread(
                self._store.purge_expired_sensitive, self._now(), self._TTL_SCRUB_BATCH)
            self._delivery_counts = await asyncio.to_thread(
                self._store.delivery_counts)
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
        self._delivery_counts = await asyncio.to_thread(
            self._store.delivery_counts)
        return len(deliveries)

    def snapshot(self) -> dict[str, int | str]:
        """Safe operational state; no body, chat ID, token, or error text."""
        state = "dead" if self._state == "dead" or self._authentication_circuit.is_dead else "running"
        return {
            "state": state,
            "last_run_claimed": self._last_run_claimed,
            **self._delivery_counts,
        }

    async def release_leases(self) -> int:
        return await asyncio.to_thread(
            self._store.release_owner_deliveries, self._worker_id)

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
