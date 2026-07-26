"""REST와 Telegram이 공유하는 운영 제어 유스케이스.

FastAPI/HTTP를 알지 않으며, 계좌 스냅샷은 일부 실패를 값으로 보존한다.
"""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from app.core.background_service import StopMode
from app.domain.notifications.ports import AccountSnapshotDeferred
from app.domain.trading.models import LiquidationResult, LiquidationTarget


@dataclass(frozen=True)
class AccountSummary:
    deposit: int | None
    available_deposit: int | None
    total_eval: int | None
    total_profit: int | None
    total_return_rate: None
    realized_pnl: int | None
    realized_pnl_confidence: str
    trading_day: date
    as_of: datetime
    source: str
    failed_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class LiquidationPreview:
    targets: tuple[LiquidationTarget, ...]
    managed_symbols: tuple[str, ...]
    unmanaged_symbols: tuple[str, ...]


class StateChangedError(ValueError):
    pass


class OperationsControl:
    CACHE_TTL = timedelta(seconds=10)

    def __init__(self, scheduler, trading, store, broker, calendar,
                 run_environment: str, *, now=None) -> None:
        self.scheduler = scheduler
        self.trading = trading
        self.store = store
        self.broker = broker
        self.calendar = calendar
        self.run_environment = run_environment
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._account_lock = asyncio.Lock()
        self._account_task = None
        self._cache = None
        self._cache_at = None
        self._telegram_snapshot_provider = None

    def set_telegram_snapshot_provider(self, provider) -> None:
        self._telegram_snapshot_provider = provider

    def scheduler_fingerprint(self) -> str:
        if self.scheduler is None:
            return "disabled"
        snapshot = self.scheduler.snapshot()
        facts = {key: snapshot.get(key)
                 for key in ("paused", "dead", "enabled")}
        return hashlib.sha256(
            json.dumps(facts, sort_keys=True).encode()).hexdigest()

    async def pause_scheduler(self):
        if self.scheduler is None:
            return {
                "enabled": False, "paused": False, "dead": False,
                "applied": False, "reason": "disabled",
            }
        self.scheduler.pause()
        return {**self.scheduler.snapshot(), "applied": True}

    async def resume_scheduler(self, expected: str | None = None):
        if expected is not None and expected != self.scheduler_fingerprint():
            raise StateChangedError("scheduler state changed")
        if self.scheduler is None:
            return {
                "enabled": False, "paused": False, "dead": False,
                "applied": False, "reason": "disabled",
            }
        self.scheduler.resume()
        return {**self.scheduler.snapshot(), "applied": True}

    async def system_status(self):
        scheduler = None if self.scheduler is None else self.scheduler.snapshot()
        trading = None if self.trading is None else self.trading.progress()
        telegram = (
            self._telegram_snapshot_provider()
            if self._telegram_snapshot_provider is not None else None)
        return {
            "scheduler": scheduler, "trading": trading,
            "telegram": telegram,
        }

    async def account_summary(self, priority: str = "interactive"):
        if priority not in {"interactive", "digest"}:
            raise ValueError("priority must be interactive or digest")
        async with self._account_lock:
            now = self._now()
            if (self._cache is not None and self._cache_at is not None
                    and now - self._cache_at < self.CACHE_TTL):
                return self._cache
            if priority == "digest":
                if self._account_task is None or self._account_task.done():
                    raise AccountSnapshotDeferred(
                        "fresh account snapshot unavailable; digest deferred")
                task = self._account_task
            elif self._account_task is None or self._account_task.done():
                self._account_task = asyncio.create_task(self._load_account())
                task = self._account_task
            else:
                task = self._account_task
        # digest는 감시/주문보다 낮은 우선순위이며 새 조회를 오래 점유하지 않는다.
        if priority == "digest":
            return await asyncio.wait_for(asyncio.shield(task), timeout=5)
        return await asyncio.shield(task)

    async def _load_account(self):
        as_of = self._now()
        deposit, balance = await asyncio.gather(
            self.broker.get_deposit(), self.broker.get_balance(),
            return_exceptions=True)
        try:
            realized, confidence = await asyncio.to_thread(
                self.store.realized_pnl_today, self.run_environment, as_of)
        except Exception:  # store 한 소스 실패도 broker 성공값을 폐기하지 않는다.
            realized, confidence = None, "estimated"
            store_failed = True
        else:
            store_failed = False
        failed = []
        if isinstance(deposit, BaseException):
            failed.append("deposit")
        if isinstance(balance, BaseException):
            failed.append("balance")
        if store_failed:
            failed.append("realized_pnl")
        summary = AccountSummary(
            deposit=(
                None if isinstance(deposit, BaseException) else deposit.total
            ),
            available_deposit=(
                None if isinstance(deposit, BaseException) else deposit.available
            ),
            total_eval=(
                None if isinstance(balance, BaseException) else balance.total_eval
            ),
            total_profit=(
                None if isinstance(balance, BaseException) else balance.total_profit
            ),
            total_return_rate=None,
            realized_pnl=realized,
            realized_pnl_confidence=confidence,
            trading_day=as_of.astimezone(self.calendar.KST).date(),
            as_of=as_of,
            source="broker+trade_store",
            failed_fields=tuple(failed),
        )
        self._cache, self._cache_at = summary, self._now()
        return summary

    async def open_positions_summary(self):
        rows, corrupted = await asyncio.to_thread(
            self.store.open_positions, self.run_environment)
        return {"positions": tuple(rows), "corrupted_rows": tuple(corrupted)}

    async def liquidation_preview(self):
        rows, _ = await asyncio.to_thread(
            self.store.open_positions, self.run_environment)
        balance = await self.broker.get_balance()
        targets = tuple(
            LiquidationTarget(pid, pos.symbol, pos.quantity)
            for pid, pos in rows if pos.state.value in {"entered", "exiting"})
        managed = tuple(sorted({target.symbol for target in targets}))
        broker_symbols = {position.symbol for position in balance.positions
                          if position.quantity > 0}
        unmanaged = tuple(sorted(broker_symbols - set(managed)))
        return LiquidationPreview(targets, managed, unmanaged)

    async def stop_new_entries(self, intent_id: str):
        if self.trading is None:
            return False
        return await self.trading.request_stop_once(
            intent_id, StopMode.STOP_NEW_ENTRIES)

    async def liquidate_managed(self, intent_id: str, targets,
                                *, expected_run_id: int | None = None):
        targets = tuple(targets)
        if not targets:
            return LiquidationResult(
                "succeeded", False, "no managed liquidation targets; no-op")
        if self.trading is None:
            return LiquidationResult(
                "needs_attention", False, "trading service is not running")
        result = await self.trading.request_managed_liquidation(
            intent_id, targets, expected_run_id=expected_run_id)
        preview = await self.liquidation_preview()
        if preview.unmanaged_symbols:
            warning = result.warning or ""
            suffix = "계좌 전체 잔고 0 아님: 미관리 잔고 존재"
            return LiquidationResult(
                result.status, False, f"{warning}; {suffix}".strip("; "))
        return result

    async def reconcile_control_intent(self, intent_id: str, targets=()) -> LiquidationResult:
        """Delegate durable Telegram liquidation recovery to TradingService.

        Command workers deliberately do not inspect broker orders or issue SELL
        requests themselves.  The trading service owns that reconciliation and
        its bounded terminal cache; a disabled trading service is an explicit
        manual-attention result rather than an implicit retry.
        """
        if self.trading is None:
            return LiquidationResult(
                "needs_attention", False, "trading service is not running")
        return await self.trading.reconcile_control_intent(intent_id, tuple(targets))
