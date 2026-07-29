"""장 마감 다이제스트의 순수 계획과 내용 조립.

이 모듈은 broker·SQLAlchemy·Telegram 전송 구현을 알지 않는다.  계좌
snapshot은 공용 OperationsControl에서만 얻고, digest 우선순위는 새 broker
요청을 시작하지 않는 계약을 따른다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol

from app.domain.broker import validate_symbol
from app.domain.errors import BrokerError
from app.domain.notifications.formatting import render_parts
from app.domain.notifications.ports import (AccountSnapshotDeferred,
                                            OperationsControlPort)


_DIGEST_ENVIRONMENTS = frozenset({"mock", "real"})
_ENVIRONMENT_LABELS = {"mock": "모의투자", "real": "🚨 실전"}
_REGIME_LABELS = {
    "risk_on": "위험선호",
    "neutral": "중립",
    "risk_off": "위험회피",
}
_FAILED_FIELD_WARNINGS = {
    "collection": "데이터 수집 결과 없음",
    "scoring": "종목 점수 계산 결과 없음",
    "analysis": "AI 분석 결과 없음",
    "trade_runs": "거래 실행 기록 없음",
    "account_snapshot": "계좌 스냅샷 조회 실패",
}
_ACCOUNT_SOURCES = frozenset({"broker+trade_store", "cached"})
_PNL_CONFIDENCES = frozenset({"estimated", "exact"})
_DIGEST_NOTICE_CODES = frozenset({
    "analysis_wait", "analysis_empty", "liquidity", "gap_guard",
    "already_held", "reentry_cooldown", "capacity", "missing_context",
    "missing_price", "requote_fallback", "quote_unstable",
    "order_attention", "unknown",
})
_SYMBOL_REQUIRED_NOTICE_CODES = frozenset({
    "liquidity", "gap_guard", "already_held", "reentry_cooldown",
    "missing_context", "missing_price", "requote_fallback",
})
_SYMBOL_OPTIONAL_NOTICE_CODES = frozenset({
    "capacity", "quote_unstable", "order_attention",
})
_MAX_DIGEST_NOTICE_KRW = 999_999_999_999_999

class CalendarPort(Protocol):
    KST: Any

    def is_trading_day(self, day: date) -> bool: ...


class DigestAuditPort(Protocol):
    def generated_digest_days(self, run_environment: str) -> tuple[date, ...]: ...

    def record_digest_skipped_stale(
            self, trading_day: date, run_environment: str, now: datetime) -> None: ...


class DigestRunStorePort(Protocol):
    def pipeline_summary(self, trading_day: date) -> "DigestSection":
        """Task 2는 거래 캘린더로 analysis_reference_expected bool을 만든다."""
        ...

    def trading_summary(self, trading_day: date) -> "DigestSection": ...


@dataclass(frozen=True)
class DigestTradeNotice:
    """Telegram에 표시 가능한 정규화된 거래 경고다."""

    code: str
    symbol: str | None = None
    observed_krw: int | None = None
    threshold_krw: int | None = None

    def __post_init__(self) -> None:
        if type(self.code) is not str or self.code not in _DIGEST_NOTICE_CODES:
            raise ValueError("unsupported digest trade notice code")
        if self.code in _SYMBOL_REQUIRED_NOTICE_CODES:
            if not _is_safe_digest_symbol(self.symbol):
                raise ValueError("digest trade notice requires a safe symbol")
        elif self.code in _SYMBOL_OPTIONAL_NOTICE_CODES:
            if self.symbol is not None and not _is_safe_digest_symbol(self.symbol):
                raise ValueError("digest trade notice symbol must be safe")
        elif self.symbol is not None:
            raise ValueError("digest trade notice does not accept a symbol")
        amounts = (self.observed_krw, self.threshold_krw)
        if self.code == "liquidity":
            if any(
                    type(value) is not int
                    or value < 0
                    or value > _MAX_DIGEST_NOTICE_KRW
                    for value in amounts):
                raise ValueError("liquidity notice amounts must be safe KRW values")
        elif any(value is not None for value in amounts):
            raise ValueError("digest trade notice amounts are only for liquidity")


def _is_safe_digest_symbol(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        validate_symbol(value)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class DigestSection:
    """허용목록 scalar만 가진 다이제스트용 read model snapshot."""

    facts: Mapping[str, str | int | bool | None]
    as_of: datetime | None
    failed_fields: tuple[str, ...] = ()
    notices: tuple[DigestTradeNotice, ...] = ()
    notice_count: int = 0

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
        if type(self.notices) is not tuple:
            raise ValueError("digest notices must be a tuple")
        if len(self.notices) > 5:
            raise ValueError("digest section has too many notices")
        if not all(isinstance(notice, DigestTradeNotice) for notice in self.notices):
            raise ValueError("digest notices must be DigestTradeNotice values")
        if type(self.notice_count) is not int or self.notice_count < 0:
            raise ValueError("digest notice_count must be a nonnegative int")
        if (self.notice_count == 0) != (len(self.notices) == 0):
            raise ValueError("digest notices and notice_count must be empty together")
        if self.notice_count < len(self.notices):
            raise ValueError("digest notice_count cannot be less than notices")


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

    def __post_init__(self) -> None:
        if self.run_environment not in _DIGEST_ENVIRONMENTS:
            raise ValueError("digest run_environment must be mock or real")
        if self.pipeline.notices or self.pipeline.notice_count:
            raise ValueError("digest pipeline section cannot contain trade notices")

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
        return _render_digest(self)

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


def _render_digest(digest: Digest) -> str:
    sections = [
        _render_digest_header(digest),
        _render_pipeline(digest.pipeline),
        _render_trading(digest.trading),
    ]
    trade_notices = _render_trade_notices(digest)
    if trade_notices:
        sections.append(trade_notices)
    sections.append(_render_account(digest))
    warnings = _digest_warnings(digest)
    if warnings:
        sections.append(
            "⚠️ 확인 필요\n"
            + "\n".join(f"- {warning}" for warning in warnings)
        )
    sections.append("🕖 다음 일정\n오늘 19:00 데이터 수집")
    return "\n\n".join(sections)


def _render_digest_header(digest: Digest) -> str:
    return (
        f"📋 장 마감 다이제스트 · "
        f"{_ENVIRONMENT_LABELS[digest.run_environment]}\n"
        f"{digest.trading_day.year}년 {digest.trading_day.month}월 "
        f"{digest.trading_day.day}일"
    )


def _render_pipeline(section: DigestSection) -> str:
    facts = section.facts
    collection_reference = _short_date(facts.get("collection_reference_day"))
    collection = (
        f"완료 · 기준 {collection_reference}"
        if facts.get("collection_status") == "done" and collection_reference
        else "확인 불가"
    )
    candidate_count = _nonnegative_int(facts.get("candidate_count"))
    scoring = (
        f"완료 · {candidate_count:,}종목"
        if facts.get("scoring_status") == "succeeded"
        and candidate_count is not None
        and _short_date(facts.get("scoring_reference_day"))
        else "확인 불가"
    )
    regime = _REGIME_LABELS.get(facts.get("market_regime"))
    analysis = (
        f"완료 · {regime}"
        if facts.get("analysis_status") == "succeeded"
        and regime is not None
        and _short_date(facts.get("analysis_score_reference_day"))
        else "확인 불가"
    )
    pick_count = _nonnegative_int(facts.get("pick_count"))
    picks = (
        "없음" if pick_count == 0
        else f"{pick_count:,}종목" if pick_count is not None
        else "확인 불가"
    )
    return "\n".join((
        "📊 오늘의 분석",
        f"데이터 수집      {collection}",
        f"종목 점수 계산   {scoring}",
        f"AI 분석          {analysis}",
        f"최종 진입 후보   {picks}",
    ))


def _render_trading(section: DigestSection) -> str:
    facts = section.facts
    return "\n".join((
        "💼 자동매매",
        "매수 주문        " + _count(facts.get("entry_order_count"), "건"),
        "매도 주문        " + _count(facts.get("exit_order_count"), "건"),
        "현재 관리 포지션 " + _count(facts.get("current_position_count"), "개"),
        "실현손익         "
        + _money(
            facts.get("realized_pnl"),
            confidence=facts.get("realized_pnl_confidence"),
        ),
    ))


def _render_trade_notices(digest: Digest) -> str:
    notices = digest.trading.notices
    if not notices:
        return ""
    lines = [_render_trade_notice(notice, digest) for notice in notices]
    extra_count = digest.trading.notice_count - len(notices)
    if extra_count:
        lines.append(f"외 {extra_count}건")
    return "⚠️ 오늘 발생한 거래 경고\n" + "\n".join(
        f"- {line}" for line in lines)


def _render_trade_notice(notice: DigestTradeNotice, digest: Digest) -> str:
    if notice.code == "analysis_wait":
        recovered = (
            digest.pipeline.facts.get("analysis_status") == "succeeded"
            and _short_date(
                digest.pipeline.facts.get("analysis_score_reference_day")) is not None
            and digest.pipeline.facts.get("analysis_reference_expected") is True
        )
        return "AI 분석 지연 후 정상 복구" if recovered else "AI 분석 결과를 제때 사용하지 못함"
    if notice.code == "analysis_empty":
        return "AI 최종 진입 후보 없음"
    if notice.code == "liquidity":
        return (f"{notice.symbol} · 유동성 기준 미달 "
                f"({_eok(notice.observed_krw)} / 기준 {_eok(notice.threshold_krw)})")
    if notice.code == "gap_guard":
        return f"{notice.symbol} · 가격 변동폭 기준 초과"
    if notice.code == "already_held":
        return f"{notice.symbol} · 이미 보유 중이라 진입하지 않음"
    if notice.code == "reentry_cooldown":
        return f"{notice.symbol} · 재진입 대기시간 적용"
    if notice.code == "capacity":
        return _optional_symbol_notice(notice, "포지션 또는 자금 한도 적용")
    if notice.code == "missing_context":
        return f"{notice.symbol} · 진입 판단 자료 부족"
    if notice.code == "missing_price":
        return f"{notice.symbol} · 가격 정보 확인 불가"
    if notice.code == "requote_fallback":
        return "최신 시세 재조회 실패 · 기존 시세 사용"
    if notice.code == "quote_unstable":
        return "보유 포지션 시세 조회 불안정"
    if notice.code == "order_attention":
        return "주문 또는 체결 상태 확인 필요"
    if notice.code == "unknown":
        return "일부 거래 상태 확인 필요"
    raise ValueError("digest trade notice renderer is missing a supported code")


def _optional_symbol_notice(notice: DigestTradeNotice, message: str) -> str:
    return f"{notice.symbol} · {message}" if notice.symbol else message


def _eok(value: int | None) -> str:
    if type(value) is not int:
        raise ValueError("liquidity notice amount must be int")
    amount = (Decimal(value) / Decimal(100_000_000)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{format(amount, 'f').rstrip('0').rstrip('.')}억"


def _render_account(digest: Digest) -> str:
    account = digest.account
    if account.source == "unavailable":
        return "💰 계좌\n계좌 스냅샷을 조회하지 못했습니다."
    if account.source not in _ACCOUNT_SOURCES:
        return "💰 계좌\n계좌 정보의 출처를 확인하지 못했습니다."
    heading = "💰 계좌"
    if account.trading_day != digest.trading_day and account.trading_day is not None:
        heading += (
            f"\n현재 계좌 · 기준 {account.trading_day.month}월 "
            f"{account.trading_day.day}일"
        )
    return "\n".join((
        heading,
        "주문 가능        " + _money(account.available_deposit),
        "보유주식 평가    " + _money(account.total_eval),
        "평가손익         " + _money(account.total_profit),
        "실현손익         "
        + _money(
            account.realized_pnl,
            confidence=account.realized_pnl_confidence,
        ),
    ))


def _digest_warnings(digest: Digest) -> tuple[str, ...]:
    warnings: list[str] = []
    unknown_section_field = False
    for field in (*digest.pipeline.failed_fields, *digest.trading.failed_fields):
        warning = _FAILED_FIELD_WARNINGS.get(field)
        if warning is None:
            unknown_section_field = True
        else:
            warnings.append(warning)
    if unknown_section_field or _has_unknown_values(digest):
        warnings.append("일부 상태 확인 불가")

    unknown_account_field = False
    for field in digest.account.failed_fields:
        warning = _FAILED_FIELD_WARNINGS.get(field)
        if warning is None:
            unknown_account_field = True
        else:
            warnings.append(warning)
    if unknown_account_field:
        warnings.append("일부 계좌 정보 확인 불가")
    warnings.extend(_account_warnings(digest))
    return tuple(dict.fromkeys(warnings))


def _account_warnings(digest: Digest) -> tuple[str, ...]:
    account = digest.account
    if account.source == "unavailable":
        return ()
    warnings: list[str] = []
    if account.source not in _ACCOUNT_SOURCES:
        warnings.append("일부 계좌 정보 확인 불가")
        return tuple(warnings)
    if any(
        type(value) is not int
        for value in (
            account.available_deposit,
            account.total_eval,
            account.total_profit,
            account.realized_pnl,
        )
    ) or account.realized_pnl_confidence not in _PNL_CONFIDENCES:
        warnings.append("일부 계좌 정보 확인 불가")
    if account.trading_day is None:
        warnings.append("계좌 기준일 확인 불가")
    elif account.trading_day != digest.trading_day:
        warnings.append("계좌 정보는 다이제스트 거래일 기준이 아님")
    return tuple(warnings)


def _has_unknown_values(digest: Digest) -> bool:
    pipeline = digest.pipeline.facts
    trading = digest.trading.facts
    pipeline_valid = (
        pipeline.get("collection_status") == "done"
        and _short_date(pipeline.get("collection_reference_day")) is not None
        and pipeline.get("scoring_status") == "succeeded"
        and _short_date(pipeline.get("scoring_reference_day")) is not None
        and _nonnegative_int(pipeline.get("candidate_count")) is not None
        and pipeline.get("analysis_status") == "succeeded"
        and _short_date(pipeline.get("analysis_score_reference_day")) is not None
        and pipeline.get("market_regime") in _REGIME_LABELS
        and _nonnegative_int(pipeline.get("pick_count")) is not None
    )
    trading_valid = all(
        _nonnegative_int(trading.get(field)) is not None
        for field in (
            "entry_order_count",
            "exit_order_count",
            "current_position_count",
        )
    )
    pnl_valid = type(trading.get("realized_pnl")) is int
    confidence_valid = trading.get("realized_pnl_confidence") in _PNL_CONFIDENCES
    return not (pipeline_valid and trading_valid and pnl_valid and confidence_valid)


def _short_date(value: object) -> str | None:
    if type(value) is not str:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return f"{parsed.month}월 {parsed.day}일"


def _nonnegative_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _count(value: object, unit: str) -> str:
    number = _nonnegative_int(value)
    return f"{number:,}{unit}" if number is not None else "확인 불가"


def _money(value: object, *, confidence: object = None) -> str:
    if type(value) is not int:
        return "확인 불가"
    if confidence is not None and confidence not in _PNL_CONFIDENCES:
        return "확인 불가"
    suffix = " (추정)" if confidence == "estimated" else ""
    return f"{value:,}원{suffix}"


def _section_payload(section: DigestSection) -> dict[str, object]:
    return {"facts": dict(section.facts), "as_of": _iso(section.as_of),
            "failed_fields": list(section.failed_fields),
            "notices": [
                {
                    "code": notice.code,
                    "symbol": notice.symbol,
                    "observed_krw": notice.observed_krw,
                    "threshold_krw": notice.threshold_krw,
                }
                for notice in section.notices
            ],
            "notice_count": section.notice_count}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def render_retained_digest(payload: Mapping[str, object]) -> str:
    """저장 당시의 digest payload만 검증해 동일 본문으로 다시 표시한다."""
    if not isinstance(payload, Mapping):
        raise ValueError("retained digest payload must be an object")
    if payload.get("version") != 1 or type(payload.get("version")) is not int:
        raise ValueError("unsupported retained digest version")
    trading_day = _required_date(payload, "trading_day")
    run_environment = _required_str(payload, "run_environment")
    return Digest(
        trading_day=trading_day,
        run_environment=run_environment,
        pipeline=_retained_section(payload, "pipeline"),
        trading=_retained_section(payload, "trading"),
        account=_retained_account(payload),
    ).body


def _retained_section(payload: Mapping[str, object], name: str) -> DigestSection:
    section = _required_object(payload, name)
    facts = _required_object(section, "facts")
    has_notices = "notices" in section
    has_notice_count = "notice_count" in section
    if has_notices != has_notice_count:
        raise ValueError("retained digest notices fields must appear together")
    if not has_notices:
        notices: tuple[DigestTradeNotice, ...] = ()
        notice_count = 0
    else:
        notices = _retained_notices(section)
        notice_count = _required_value(section, "notice_count")
    return DigestSection(
        facts=dict(facts),
        as_of=_optional_datetime(section, "as_of"),
        failed_fields=_string_tuple(section, "failed_fields"),
        notices=notices,
        notice_count=notice_count,
    )


def _retained_notices(section: Mapping[str, object]) -> tuple[DigestTradeNotice, ...]:
    value = _required_value(section, "notices")
    if not isinstance(value, list):
        raise ValueError("retained digest notices must be a list")
    required_keys = {"code", "symbol", "observed_krw", "threshold_krw"}
    notices: list[DigestTradeNotice] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != required_keys:
            raise ValueError("retained digest notice must have the safe schema")
        notices.append(DigestTradeNotice(
            code=item["code"],
            symbol=item["symbol"],
            observed_krw=item["observed_krw"],
            threshold_krw=item["threshold_krw"],
        ))
    return tuple(notices)


def _retained_account(payload: Mapping[str, object]) -> DigestAccount:
    account = _required_object(payload, "account")
    amounts: list[int | None] = []
    for field in ("available_deposit", "total_eval", "total_profit", "realized_pnl"):
        value = _required_value(account, field)
        if value is not None and type(value) is not int:
            raise ValueError(f"retained digest account {field} must be int or null")
        amounts.append(value)
    trading_day = _required_value(account, "trading_day")
    if trading_day is not None:
        if type(trading_day) is not str:
            raise ValueError("retained digest account trading_day must be string or null")
        try:
            trading_day = date.fromisoformat(trading_day)
        except ValueError as exc:
            raise ValueError("invalid retained digest account trading_day") from exc
    return DigestAccount(
        *amounts,
        realized_pnl_confidence=_required_str(account, "realized_pnl_confidence"),
        source=_required_str(account, "source"),
        failed_fields=_string_tuple(account, "failed_fields"),
        as_of=_optional_datetime(account, "as_of"),
        trading_day=trading_day,
    )


def _required_object(payload: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = _required_value(payload, name)
    if not isinstance(value, Mapping):
        raise ValueError(f"retained digest {name} must be an object")
    return value


def _required_value(payload: Mapping[str, object], name: str) -> object:
    if name not in payload:
        raise ValueError(f"retained digest is missing {name}")
    return payload[name]


def _required_str(payload: Mapping[str, object], name: str) -> str:
    value = _required_value(payload, name)
    if type(value) is not str or not value:
        raise ValueError(f"retained digest {name} must be a non-empty string")
    return value


def _required_date(payload: Mapping[str, object], name: str) -> date:
    value = _required_value(payload, name)
    if type(value) is not str:
        raise ValueError(f"retained digest {name} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid retained digest {name}") from exc


def _optional_datetime(payload: Mapping[str, object], name: str) -> datetime | None:
    value = _required_value(payload, name)
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError(f"retained digest {name} must be an ISO datetime or null")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid retained digest {name}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"retained digest {name} must be timezone-aware")
    return parsed


def _string_tuple(payload: Mapping[str, object], name: str) -> tuple[str, ...]:
    value = _required_value(payload, name)
    if not isinstance(value, list) or not all(type(item) is str for item in value):
        raise ValueError(f"retained digest {name} must be a string list")
    return tuple(value)
