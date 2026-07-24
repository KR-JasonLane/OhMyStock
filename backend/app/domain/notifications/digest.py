"""장 마감 다이제스트의 순수 계획과 내용 조립.

이 모듈은 broker·SQLAlchemy·Telegram 전송 구현을 알지 않는다.  계좌
snapshot은 공용 OperationsControl에서만 얻고, digest 우선순위는 새 broker
요청을 시작하지 않는 계약을 따른다.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol

from app.domain.errors import BrokerError
from app.domain.notifications.formatting import render_parts
from app.domain.notifications.ports import (AccountSnapshotDeferred,
                                            OperationsControlPort)

class CalendarPort(Protocol):
    KST: Any

    def is_trading_day(self, day: date) -> bool: ...


class DigestAuditPort(Protocol):
    def generated_digest_days(self, run_environment: str) -> tuple[date, ...]: ...

    def record_digest_skipped_stale(
            self, trading_day: date, run_environment: str, now: datetime) -> None: ...


class DigestRunStorePort(Protocol):
    def pipeline_summary(self, trading_day: date) -> "DigestSection": ...

    def trading_summary(self, trading_day: date) -> "DigestSection": ...


@dataclass(frozen=True)
class DigestSection:
    """허용목록 scalar만 가진 다이제스트용 read model snapshot."""

    facts: Mapping[str, str | int | bool | None]
    as_of: datetime | None
    failed_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.as_of is not None and (
                self.as_of.tzinfo is None or self.as_of.utcoffset() is None):
            raise ValueError("section as_of must be timezone-aware")
        if len(self.facts) > 24:
            raise ValueError("digest section has too many facts")
        checked: dict[str, str | int | bool | None] = {}
        for key, value in self.facts.items():
            if not isinstance(key, str) or not key.isidentifier() or len(key) > 48:
                raise ValueError("digest fact key must be a short identifier")
            if any(fragment in key.lower()
                   for fragment in ("token", "secret", "password", "account")):
                raise ValueError("sensitive digest fact key is not allowed")
            if value is not None and type(value) not in {str, int, bool}:
                raise ValueError("digest facts must be scalar")
            if isinstance(value, str) and len(value) > 96:
                raise ValueError("digest string fact is too long")
            checked[key] = value
        object.__setattr__(self, "facts", checked)
        object.__setattr__(self, "failed_fields", tuple(self.failed_fields))


@dataclass(frozen=True)
class DigestAccount:
    available_deposit: int | None
    total_eval: int | None
    total_profit: int | None
    realized_pnl: int | None
    realized_pnl_confidence: str
    source: str
    failed_fields: tuple[str, ...]
    as_of: datetime | None
    trading_day: date | None

    def __post_init__(self) -> None:
        for value in (self.available_deposit, self.total_eval,
                      self.total_profit, self.realized_pnl):
            if value is not None and type(value) is not int:
                raise ValueError("digest account amounts must be int or None")
        if self.as_of is not None and (
                self.as_of.tzinfo is None or self.as_of.utcoffset() is None):
            raise ValueError("digest account as_of must be timezone-aware")
        if len(self.failed_fields) > 12 or any(
                not isinstance(field, str) or len(field) > 48
                for field in self.failed_fields):
            raise ValueError("invalid digest account failed_fields")


@dataclass(frozen=True)
class Digest:
    trading_day: date
    run_environment: str
    pipeline: DigestSection
    trading: DigestSection
    account: DigestAccount

    @property
    def idempotency_key(self) -> str:
        return f"digest:{self.run_environment}:{self.trading_day.isoformat()}"

    @property
    def payload(self) -> dict[str, object]:
        return {
            "version": 1,
            "trading_day": self.trading_day.isoformat(),
            "run_environment": self.run_environment,
            "pipeline": _section_payload(self.pipeline),
            "trading": _section_payload(self.trading),
            "account": {
                "available_deposit": self.account.available_deposit,
                "total_eval": self.account.total_eval,
                "total_profit": self.account.total_profit,
                "realized_pnl": self.account.realized_pnl,
                "realized_pnl_confidence": self.account.realized_pnl_confidence,
                "source": self.account.source,
                "failed_fields": list(self.account.failed_fields),
                "as_of": _iso(self.account.as_of),
                "trading_day": (self.account.trading_day.isoformat()
                                if self.account.trading_day else None),
            },
        }

    @property
    def body(self) -> str:
        account = self.account
        if account.source == "unavailable":
            account_line = "계좌 스냅샷: 조회 불가 (" + ", ".join(account.failed_fields) + ")"
        else:
            account_line = (
                "계좌 스냅샷(" + _as_of(account.as_of) + "): 예수금 "
                + _amount(account.available_deposit)
                + ", 총평가 " + _amount(account.total_eval)
                + ", 평가손익 " + _amount(account.total_profit)
                + ", 실현손익 " + _amount(account.realized_pnl))
            if account.failed_fields:
                account_line += " (실패 필드: " + ", ".join(account.failed_fields) + ")"
            if account.trading_day is not None and account.trading_day != self.trading_day:
                account_line += " (현재 조회 기준일: " + account.trading_day.isoformat() + ")"
        return "\n".join((
            f"다이제스트 ID: {self.idempotency_key}",
            f"장 마감 다이제스트 {self.trading_day.isoformat()}",
            "파이프라인(" + _as_of(self.pipeline.as_of) + "): "
            + _compact_json(self.pipeline.facts) + _failed(self.pipeline.failed_fields),
            "거래(" + _as_of(self.trading.as_of) + "): "
            + _compact_json(self.trading.facts) + _failed(self.trading.failed_fields),
            account_line,
            "19:00 수집: 예정",
        ))

    @property
    def bodies(self) -> tuple[str, ...]:
        correlation_id = self.idempotency_key.replace(":", "-")
        return tuple(part.text for part in render_parts(self.body, correlation_id))


class DigestPlanner:
    """16:10 KST 이후 최근 7 거래일을 오래된 순으로 선택한다."""

    _CLOSE_TIME = time(16, 10)
    _LOOKBACK_DAYS = 7

    def __init__(self, calendar: CalendarPort, audit: DigestAuditPort,
                 run_environment: str) -> None:
        self._calendar = calendar
        self._audit = audit
        self._run_environment = run_environment
        self._marked_generated: set[date] = set()

    def mark_generated(self, trading_day: date) -> None:
        """테스트 및 같은 process loop의 중복 방지용 즉시 표식.

        영속 SSOT는 outbox idempotency key이며 다음 process에서는 audit port가
        이를 다시 읽는다.
        """
        self._marked_generated.add(trading_day)

    def due_dates(self, now: datetime) -> tuple[date, ...]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        kst_now = now.astimezone(self._calendar.KST)
        if (not self._calendar.is_trading_day(kst_now.date())
                or kst_now.time() < self._CLOSE_TIME):
            return ()

        recent = self._recent_trading_days(kst_now.date())
        generated = set(self._audit.generated_digest_days(self._run_environment))
        generated.update(self._marked_generated)
        latest = max(generated, default=None)
        if latest is not None and latest < recent[0]:
            self._record_stale_gap(latest, recent[0], kst_now, generated)
        # Window 안에서는 high-water mark와 무관하게 날짜별 outbox 존재를
        # 비교해 모든 hole을 오래된 순서로 복구한다. mark 이전 gap만 audit한다.
        return tuple(day for day in recent if day not in generated)

    def _recent_trading_days(self, today: date) -> tuple[date, ...]:
        found: list[date] = []
        day = today
        while len(found) < self._LOOKBACK_DAYS:
            if self._calendar.is_trading_day(day):
                found.append(day)
            day -= timedelta(days=1)
        return tuple(reversed(found))

    def _record_stale_gap(self, latest: date, cutoff: date, now: datetime,
                          generated: set[date]) -> None:
        day = latest + timedelta(days=1)
        while day < cutoff:
            if self._calendar.is_trading_day(day) and day not in generated:
                self._audit.record_digest_skipped_stale(
                    day, self._run_environment, now)
            day += timedelta(days=1)


class DigestBuilder:
    """계좌 snapshot 실패를 금액 0이 아닌 명시적 unavailable로 표현한다."""

    _TIMEOUT_S = 5

    def __init__(self, control: OperationsControlPort, runs: DigestRunStorePort,
                 run_environment: str, *, now: Callable[[], datetime]) -> None:
        self._control = control
        self._runs = runs
        self._run_environment = run_environment
        self._now = now

    async def build(self, trading_day: date) -> Digest:
        try:
            snapshot = await asyncio.wait_for(
                self._control.account_summary(priority="digest"),
                timeout=self._TIMEOUT_S)
        except (TimeoutError, ConnectionError, BrokerError):
            account = _unavailable_account()
        except AccountSnapshotDeferred:
            account = _unavailable_account()
        else:
            snapshot_as_of = getattr(snapshot, "as_of", None)
            if snapshot_as_of is not None and (
                    snapshot_as_of.tzinfo is None or snapshot_as_of.utcoffset() is None):
                snapshot_as_of = None
            account = DigestAccount(
                available_deposit=getattr(snapshot, "available_deposit", None),
                total_eval=getattr(snapshot, "total_eval", None),
                total_profit=getattr(snapshot, "total_profit", None),
                realized_pnl=getattr(snapshot, "realized_pnl", None),
                realized_pnl_confidence=getattr(
                    snapshot, "realized_pnl_confidence", "unknown"),
                source=getattr(snapshot, "source", "unknown"),
                failed_fields=tuple(getattr(snapshot, "failed_fields", ())),
                as_of=snapshot_as_of,
                trading_day=getattr(snapshot, "trading_day", None),
            )
        return Digest(
            trading_day=trading_day,
            run_environment=self._run_environment,
            pipeline=await asyncio.to_thread(
                self._runs.pipeline_summary, trading_day),
            trading=await asyncio.to_thread(
                self._runs.trading_summary, trading_day),
            account=account,
        )


def _unavailable_account() -> DigestAccount:
    return DigestAccount(
        available_deposit=None, total_eval=None, total_profit=None,
        realized_pnl=None, realized_pnl_confidence="unknown",
        source="unavailable", failed_fields=("account_snapshot",),
        as_of=None, trading_day=None)


def _amount(value: int | None) -> str:
    return "조회 불가" if value is None else f"{value:,}원"


def _compact_json(value: Mapping[str, object]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _section_payload(section: DigestSection) -> dict[str, object]:
    return {"facts": dict(section.facts), "as_of": _iso(section.as_of),
            "failed_fields": list(section.failed_fields)}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _as_of(value: datetime | None) -> str:
    return _iso(value) or "조회 불가"


def _failed(fields: tuple[str, ...]) -> str:
    return " (누락 필드: " + ", ".join(fields) + ")" if fields else ""
