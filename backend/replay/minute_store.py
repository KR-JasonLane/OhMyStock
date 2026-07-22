"""분봉 저장소 로더(스펙 §6 2단 구조의 ② 파싱 단계 + 재생 조회).

- 기동 시 sqlite(minute_raw 원문 JSON)를 메모리 적재(§4 — 요청 경로에
  DB I/O 없음: 블로킹이 결함 주입 타이밍을 왜곡하지 않게). **재생 서버는
  symbols/since 필터로 앵커 이후 구간만 부분 적재가 기본**(전량 적재 실측
  842MB/9.5s — 아키텍트 R2 #2), 전량은 오프라인 분석(게이트) 전용.
- 파싱 실패는 fail-loud(P2 degenerate 캔들 전례 — 012510이 대상에 포함된
  이유). 단, 명시적 스킵 카운트는 노출한다(침묵 금지).
- **미래 누출 구조적 차단(§5 제1 불변식)**: 조회 API는 `ts 이하`만 접근
  가능한 형태뿐 — "이후" 데이터를 주는 메서드 자체가 없다.

필드명 계약: R1 실측(ka10080)이 확정한다 — 로더는 실측 확정 전까지
후보 필드명(cntr_tm/cur_prc 계열 + ka10081 계열)을 순서대로 시도하고,
어느 것도 아니면 fail-loud(형태 드리프트를 조용히 넘기지 않는다)."""

import bisect
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from replay.clock import KST


@dataclass(frozen=True)
class MinuteCandle:
    symbol: str
    ts: datetime          # 분봉 시각(KST-aware, 분 단위)
    open: int
    high: int
    low: int
    close: int
    volume: int


# 필드명 — **R1 실측 확정**(2026-07-22, replay-ka10080-probe.txt):
# cntr_tm(YYYYMMDDHHMMSS) + open_pric/high_pric/low_pric/cur_prc(±부호)/
# trde_qty. 2차 후보(dt)는 형태 드리프트 감지용 폴백으로만 유지.
# acc_trde_qty/pred_pre/pred_pre_sig는 **의도적으로 버린다**(§6 스키마·§8
# 매칭에 불필요 — broker-api R1 Minor5: R4에서 등락률 재현이 필요해지면
# minute_raw 원문 JSON에서 재추출 가능).
_FIELD_CANDIDATES: tuple[dict, ...] = (
    {"ts": "cntr_tm", "open": "open_pric", "high": "high_pric",
     "low": "low_pric", "close": "cur_prc", "volume": "trde_qty"},
    {"ts": "dt", "open": "open_pric", "high": "high_pric",
     "low": "low_pric", "close": "cur_prc", "volume": "trde_qty"},
)


def _to_int(value) -> int:
    """키움 수치는 부호/제로패딩 문자열("-00012500" 등) — 절대값 정수화.
    (분봉 가격의 부호는 등락 표기 관례 — 가격 자체는 양수)"""
    text = str(value).strip()
    if not text:
        raise ValueError("empty numeric field")
    return abs(int(text))


def _parse_ts(raw: str) -> datetime:
    text = str(raw).strip()
    if len(text) == 14:  # YYYYMMDDHHMMSS
        return datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=KST)
    if len(text) == 12:  # YYYYMMDDHHMM
        return datetime.strptime(text, "%Y%m%d%H%M").replace(tzinfo=KST)
    raise ValueError(f"unrecognized minute ts format: {raw!r}")


def _ensure_kst(ts: datetime) -> datetime:
    """조회 시각 KST 정규화 — naive는 KST로 간주, aware는 변환.
    astimezone() 단독은 naive를 **시스템 로컬 tz로 간주**해 R3/R4의 외부
    입력에서 market_calendar 실버그와 동일 클래스가 재발한다(개발자 R2 #1 —
    app.core.market_calendar._as_kst와 동일 분기)."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=KST)
    return ts.astimezone(KST)


class MinuteStore:
    """심볼별 분봉 시계열(오름차순 정렬) — bisect 조회."""

    def __init__(self) -> None:
        self._series: dict[str, list[MinuteCandle]] = {}
        self._keys: dict[str, list[datetime]] = {}
        self.skipped = 0   # 파싱 스킵(결측 필드 행) — 침묵 금지, 로더가 보고

    @classmethod
    def load(cls, sqlite_path: Path | str,
             symbols: list[str] | None = None,
             since: datetime | None = None) -> "MinuteStore":
        """적재. symbols/since 필터(아키텍트 R2 #2 — 전량 적재 실측 842MB/
        9.5s: 재생 서버는 앵커 이후 구간만 적재해 메모리·기동시간을 줄인다.
        오프라인 분석(게이트)은 무필터 전량 허용)."""
        store = cls()
        conn = sqlite3.connect(str(sqlite_path))
        try:
            if symbols:
                marks = ",".join("?" for _ in symbols)
                rows = conn.execute(
                    f"SELECT symbol, row FROM minute_raw WHERE symbol IN "
                    f"({marks}) ORDER BY symbol, seq", symbols).fetchall()
            else:
                rows = conn.execute(
                    "SELECT symbol, row FROM minute_raw "
                    "ORDER BY symbol, seq").fetchall()
        finally:
            conn.close()
        since_kst = _ensure_kst(since) if since is not None else None
        if not rows:
            raise ValueError(f"no minute_raw rows in {sqlite_path}")
        # 필드셋 감지는 "인식되는 행이 나올 때까지" 진행 — 첫 행이 degenerate/
        # 이형이어도 전체 로드가 죽지 않는다(개발자 R2 #3: 행 단위 garbage와
        # 전체 형태 드리프트의 구분). 끝까지 미감지면 드리프트로 fail-loud.
        fields = None
        by_symbol: dict[str, dict[datetime, MinuteCandle]] = {}
        for symbol, raw in rows:
            record = json.loads(raw)
            if fields is None:
                try:
                    fields = _detect_fields(record)
                except ValueError:
                    store.skipped += 1
                    continue
            try:
                candle = _parse_row(symbol, record, fields)
            except ValueError:
                # degenerate 행(빈 필드 등 — P2 전례): 행 단위 스킵+카운트
                store.skipped += 1
                continue
            if since_kst is not None and candle.ts < since_kst:
                continue
            # 중복 ts 최후 승리 — 페이지 경계 중복 수집 시 나중 seq(재조회분)
            # 채택. 정책은 테스트로 고정(개발자 R2 #4).
            by_symbol.setdefault(symbol, {})[candle.ts] = candle
        for symbol, candles in by_symbol.items():
            series = sorted(candles.values(), key=lambda c: c.ts)
            store._series[symbol] = series
            store._keys[symbol] = [c.ts for c in series]
        if not store._series:
            raise ValueError(
                "all minute rows failed to parse/detect — field drift?")
        return store

    @property
    def symbols(self) -> list[str]:
        return sorted(self._series)

    def span(self, symbol: str) -> tuple[datetime, datetime]:
        series = self._series[symbol]
        return series[0].ts, series[-1].ts

    def candles(self, symbol: str) -> tuple[MinuteCandle, ...]:
        """전체 시계열(오름차순) — **오프라인 분석 전용**(커버리지 게이트 등,
        개발자 R2 #5: 내부 접근 우회의 정식 API 승격). 라이브 재생 경로는
        candle_at/last_at_or_before만 사용할 것 — 이 메서드를 재생 응답에
        쓰면 §5 미래 누출 불변식이 깨진다(호출자 책임 경계 명시)."""
        return tuple(self._series.get(symbol, ()))

    def candle_at(self, symbol: str, ts: datetime) -> MinuteCandle | None:
        """ts가 속한 분의 봉(정확히 그 분). 없으면 None(결측 분)."""
        minute = _ensure_kst(ts).replace(second=0, microsecond=0)
        keys = self._keys.get(symbol)
        if not keys:
            return None
        idx = bisect.bisect_left(keys, minute)
        if idx < len(keys) and keys[idx] == minute:
            return self._series[symbol][idx]
        return None

    def last_at_or_before(self, symbol: str, ts: datetime) -> MinuteCandle | None:
        """ts 이하의 마지막 봉 — 결측 분(무거래 분 — 실측: 게이트 스크립트의
        갭 통계, replay-ka10080-coverage.txt)의 직전가 유지 정책과 재생
        현재가 조회의 기본형. **ts 초과 접근 불가**(§5). 직전가 유지가
        ka10095 실시간 스냅샷 의미와 동일하다는 가정은 저유동 심볼 확보 시
        ka10095 프로브로 재검(§12 — 트레이더 R2 Minor4)."""
        keys = self._keys.get(symbol)
        if not keys:
            return None
        idx = bisect.bisect_right(keys, _ensure_kst(ts))
        if idx == 0:
            return None
        return self._series[symbol][idx - 1]


def _detect_fields(record: dict) -> dict:
    for candidate in _FIELD_CANDIDATES:
        if all(name in record for name in candidate.values()):
            return candidate
    raise ValueError(
        f"minute row fields unrecognized (drift?): keys={sorted(record)[:12]}")


def _parse_row(symbol: str, record: dict, fields: dict) -> MinuteCandle:
    candle = MinuteCandle(
        symbol=symbol,
        ts=_parse_ts(record[fields["ts"]]),
        open=_to_int(record[fields["open"]]),
        high=_to_int(record[fields["high"]]),
        low=_to_int(record[fields["low"]]),
        close=_to_int(record[fields["close"]]),
        volume=_to_int(record[fields["volume"]]),
    )
    if candle.close <= 0 or candle.high < candle.low:
        raise ValueError(f"degenerate candle {symbol} {candle.ts}")
    return candle
