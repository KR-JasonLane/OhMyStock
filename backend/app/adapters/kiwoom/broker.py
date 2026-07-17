"""BrokerPort의 키움 구현. TR id·필드명 등 키움 상세는 이 파일 밖으로 새지 않는다.
응답 필드명은 비공식 자료 기반이며 라이브 스모크로 실측 검증한다 (spec §5)."""

from collections.abc import Callable
from contextlib import aclosing
from datetime import date, datetime
from decimal import Decimal

from app.adapters.kiwoom.auth import KST
from app.adapters.kiwoom.client import KiwoomHttpClient
from app.adapters.kiwoom.errors import BrokerError
from app.domain.broker import Candle, Quote


def _to_int(s: str | None) -> int:
    """키움 숫자 문자열('+71000', '-0000050000', '') → int (부호 보존)."""
    if s is None or not s.strip():
        return 0
    return int(s)


def _to_price(s: str | None) -> int:
    """가격 필드 — 키움은 등락 방향을 부호로 실어 보내므로 절대값을 취한다."""
    return abs(_to_int(s))


def _to_decimal(s: str | None) -> Decimal:
    """키움 등락율 문자열('+1.25', '-0.50', '') → Decimal. Decimal()이 선행 '+'를
    그대로 파싱하므로 별도 부호 제거가 필요 없다."""
    if s is None or not s.strip():
        return Decimal("0")
    return Decimal(s)


class KiwoomBroker:
    def __init__(
        self,
        client: KiwoomHttpClient,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._client = client
        self._today = today or (lambda: datetime.now(KST).date())

    async def get_quote(self, symbol: str) -> Quote:
        data, _, _ = await self._client.call("stkinfo", "ka10001", {"stk_cd": symbol})
        try:
            return Quote(
                symbol=symbol,
                name=data["stk_nm"],
                price=_to_price(data["cur_prc"]),
                change_rate=_to_decimal(data.get("flu_rt")),
                volume=_to_int(data.get("trde_qty")),
            )
        except (KeyError, ValueError, ArithmeticError) as exc:
            raise BrokerError(
                f"unexpected response schema [ka10001]: {type(exc).__name__}") from exc

    async def get_daily_candles(self, symbol: str, count: int) -> list[Candle]:
        # upd_stkpc_tp="1" — 수정주가 적용. BrokerPort.get_daily_candles 계약(도메인
        # 문서화)에 따라 반환 가격은 반드시 수정주가여야 한다.
        # base_dt는 빈 문자열을 허용하지 않는다(실측 정정) — 오늘(KST) 날짜를 채워
        # 그 날짜를 기준으로 과거 방향 조회를 시작한다.
        base_dt = self._today().strftime("%Y%m%d")
        body = {"stk_cd": symbol, "base_dt": base_dt, "upd_stkpc_tp": "1"}
        rows: list[dict] = []
        async with aclosing(self._client.call_paged("chart", "ka10081", body)) as pages:
            async for page in pages:
                rows.extend(page.get("stk_dt_pole_chart_qry") or [])
                if len(rows) >= count:
                    break
        rows = rows[:count]  # 응답은 최신→과거 순
        try:
            candles = [
                Candle(
                    symbol=symbol,
                    date=datetime.strptime(r["dt"], "%Y%m%d").date(),
                    open=_to_price(r["open_pric"]),
                    high=_to_price(r["high_pric"]),
                    low=_to_price(r["low_pric"]),
                    close=_to_price(r["cur_prc"]),
                    volume=_to_int(r.get("trde_qty")),
                )
                for r in rows
            ]
        except (KeyError, ValueError, ArithmeticError) as exc:
            raise BrokerError(
                f"unexpected response schema [ka10081]: {type(exc).__name__}") from exc
        candles.sort(key=lambda c: c.date)  # 과거→최신
        return candles

    async def aclose(self) -> None:
        await self._client.aclose()
