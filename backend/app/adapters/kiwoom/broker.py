"""BrokerPort/OrderPort의 키움 구현. TR id·필드명 등 키움 상세는 이 파일 밖으로
새지 않는다. 응답 필드명은 비공식 자료 기반이며 라이브 스모크로 실측 검증한다
(spec §5).

**페이지 초과 정책(P5-T4 아키텍트 패널 — 한 곳에 정리):**
  - 카탈로그류(list_instruments 등): call_paged 상한 초과 시 **조용한 절단 +
    경고 로그** — 대량 목록의 부분 수집은 멱등 재수집으로 복구 가능.
  - 주문/계좌류(get_open_orders): cont-yn=Y면 **fail-loud** — 미체결 누락은
    취소 실패·포지션 방치로 직결되므로 부분 결과를 돌려주지 않는다.
  다음 기여자는 메서드 성격에 맞는 쪽을 따를 것(교차 복제 금지)."""

import logging
from collections.abc import Callable
from contextlib import aclosing
from datetime import date, datetime
from decimal import Decimal

from app.adapters.kiwoom.auth import KST
from app.adapters.kiwoom.client import KiwoomHttpClient
from app.adapters.kiwoom import orders as _orders
from app.domain.broker import (Balance, Candle, Deposit, Instrument,
                               MarketData, OpenOrder, OrderAck, OrderRequest,
                               Position, Quote, Sector, validate_symbol)
from app.domain.errors import BrokerError

# ka10095(관심종목정보) — G1 실측 확정(2026-07-22): 다종목 구분자 파이프('|'),
# 맨코드, 1회 최대 100종목(101→rc=5), 리스트 키 atn_stk_infr, 최우선 호가
# sel_bid/buy_bid 포함. 더미/결측 종목은 빈 행(stk_cd="")으로 섞여 온다 — 필터.
_QUOTES_BATCH_MAX = 100

# ka10099(전체종목조회) 시장구분 — 실측 확정 (스파이크 2026-07-17).
_MRKT_TP = {"kospi": "0", "kosdaq": "10", "etf": "8"}

# ka10101(업종코드리스트)/ka20002(업종별주가) 공용 시장구분 — 실측 확정.
# ka10099와 코스닥 값이 다르다(10 vs 1)는 점에 주의.
_SECTOR_MARKETS = (("kospi", "0"), ("kosdaq", "1"))
_SECTOR_MRKT_TP = dict(_SECTOR_MARKETS)

logger = logging.getLogger(__name__)


def _normalize_symbol(raw: str) -> str:
    """키움 종목코드 정규화 — 'A' 접두가 있으면 제거하고 6자리 ASCII 영숫자인지
    검증한다. 실측 정정(2026-07-17 라이브 스모크): KRX 코드는 순수 숫자만이
    아니다 — 예를 들어 ka10099의 kospi(mrkt_tp="0") 목록에는 일반 주식(예:
    '000020')과 함께 ETF(marketCode="8")가 섞여 나오며, ETF 코드는 '0000D0'처럼
    영문자를 포함한다. 당초(Phase 1) isdigit() 전용 검증은 이 알파벳 혼합
    코드를 거부했다 — 6자리 ASCII 영숫자로 완화한다. isascii()를 함께 요구하는
    이유: isalnum() 단독으로는 유니코드 문자(한글 등)도 "영숫자"로 통과시켜
    fail-loud 가드가 무력화되므로, ASCII 범위로 제한해 원래의 방어 목적을
    유지한다. fail-loud: 그래도 형식이 맞지 않으면(무음 실패 대신) ValueError로
    표면화한다."""
    code = raw.removeprefix("A")
    try:
        return validate_symbol(code)  # 형식 규칙은 도메인 소유(P5-T4 보안 패널)
    except ValueError:
        raise ValueError(f"unexpected symbol format: {raw!r}") from None


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


def _parse_position(row: dict) -> Position:
    """잔고 응답의 개별 종목 행 → Position. stk_cd 정규화는 _normalize_symbol에
    위임한다 (fail-loud: 형식 불일치 시 ValueError)."""
    return Position(
        symbol=_normalize_symbol(row["stk_cd"]),
        name=row["stk_nm"],
        quantity=_to_int(row.get("rmnd_qty")),
        avg_price=_to_price(row.get("pur_pric")),
        current_price=_to_price(row.get("cur_prc")),
        eval_amount=_to_int(row.get("evlt_amt")),
    )


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
        except (KeyError, ValueError, ArithmeticError, TypeError, AttributeError) as exc:
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
        except (KeyError, ValueError, ArithmeticError, TypeError, AttributeError) as exc:
            raise BrokerError(
                f"unexpected response schema [ka10081]: {type(exc).__name__}") from exc
        candles.sort(key=lambda c: c.date)  # 과거→최신
        return candles

    async def get_deposit(self) -> Deposit:
        data, _, _ = await self._client.call("acnt", "kt00001", {"qry_tp": "3"})
        try:
            # entr/ord_alow_amt는 핵심 금액 필드 — 누락 시 .get()의 silent-0 대신
            # 대괄호 인덱싱으로 fail-loud한다 (KeyError → BrokerError).
            return Deposit(
                total=_to_int(data["entr"]),
                available=_to_int(data["ord_alow_amt"]),
            )
        except (KeyError, ValueError, ArithmeticError, TypeError, AttributeError) as exc:
            raise BrokerError(
                f"unexpected response schema [kt00001]: {type(exc).__name__}") from exc

    async def get_balance(self) -> Balance:
        data, _, _ = await self._client.call(
            "acnt", "kt00018", {"qry_tp": "1", "dmst_stex_tp": "KRX"})
        try:
            # tot_evlt_amt/tot_evlt_pl은 핵심 금액 필드 — 누락 시 fail-loud (위와 동일
            # 이유). acnt_evlt_remn_indv_tot는 포지션이 없는 계좌에서 정당하게 부재할
            # 수 있으므로 .get(...) or [] 유지.
            positions = tuple(
                _parse_position(row)
                for row in data.get("acnt_evlt_remn_indv_tot") or []
            )
            return Balance(
                positions=positions,
                total_eval=_to_int(data["tot_evlt_amt"]),
                total_profit=_to_int(data["tot_evlt_pl"]),
            )
        except (KeyError, ValueError, ArithmeticError, TypeError, AttributeError) as exc:
            raise BrokerError(
                f"unexpected response schema [kt00018]: {type(exc).__name__}") from exc

    async def list_instruments(self, market: str) -> list[Instrument]:
        # call_paged는 max_pages(기본 50) 소진 시 예외 없이 지금까지 모은 페이지만
        # 반환하고 경고 로그만 남긴다(client.py) — 카탈로그 조회에서는 이 truncation이
        # 조용히 불완전한 목록으로 흘러갈 위험이 있다. 실측(2026-07-17)으로는 코스피
        # 2478 / 코스닥 1821 / etf 1147행이 각각 단일 페이지(cont-yn=N)로 반환됨을
        # 확인했으나, 상한을 넘는 방어책은 아직 없다 — 완결성이 중요해지면(Phase 3)
        # 페이지 소진 여부를 신호로 노출하는 개선을 검토할 것.
        if market not in _MRKT_TP:
            raise ValueError(f"unknown market: {market}")
        items: list[Instrument] = []
        try:
            async with aclosing(self._client.call_paged(
                    "stkinfo", "ka10099", {"mrkt_tp": _MRKT_TP[market]})) as pages:
                async for page in pages:
                    for row in page.get("list") or []:
                        # 실측: mrkt_tp="0"(kospi) 요청 응답에도 다른 marketCode(예:
                        # ETF="8")를 가진 행이 섞여 온다 — 요청한 시장코드와 실제
                        # 행의 marketCode가 다르면 건너뛴다. 그렇지 않으면 ETF가
                        # market="kospi"로 오라벨된 Instrument로 저장된다.
                        if row.get("marketCode") != _MRKT_TP[market]:
                            continue
                        items.append(Instrument(
                            symbol=_normalize_symbol(row["code"]),
                            name=row["name"],
                            market=market,
                            instrument_type=str(row.get("kind") or ""),
                            state=(row.get("state") or "").strip(),
                            audit_info=(row.get("auditInfo") or "").strip(),
                        ))
        except (KeyError, ValueError, ArithmeticError, TypeError, AttributeError) as exc:
            raise BrokerError(
                f"unexpected response schema [ka10099]: {type(exc).__name__}") from exc
        return items

    async def list_sectors(self) -> list[Sector]:
        sectors: list[Sector] = []
        try:
            for market, mrkt_tp in _SECTOR_MARKETS:
                async with aclosing(self._client.call_paged(
                        "stkinfo", "ka10101", {"mrkt_tp": mrkt_tp})) as pages:
                    async for page in pages:
                        for row in page.get("list") or []:
                            sectors.append(Sector(code=row["code"], market=market,
                                                  name=row["name"]))
        except (KeyError, ValueError, ArithmeticError, TypeError, AttributeError) as exc:
            raise BrokerError(
                f"unexpected response schema [ka10101]: {type(exc).__name__}") from exc
        return sectors

    async def list_sector_members(self, sector_code: str, market: str) -> list[str]:
        if market not in _SECTOR_MRKT_TP:
            raise ValueError(f"unknown market: {market}")
        body = {"mrkt_tp": _SECTOR_MRKT_TP[market], "inds_cd": sector_code,
                "stex_tp": "1"}  # stex_tp 누락 시 1511 에러 (실측 확정). "1" 고정값이
        # kospi/kosdaq 양쪽 모두 유효함을 test_live_업종코드와_구성종목으로 실측 완료.
        members: list[str] = []
        try:
            async with aclosing(self._client.call_paged(
                    "sect", "ka20002", body)) as pages:
                async for page in pages:
                    for row in page.get("inds_stkpc") or []:
                        members.append(_normalize_symbol(row["stk_cd"]))
        except (KeyError, ValueError, ArithmeticError, TypeError, AttributeError) as exc:
            raise BrokerError(
                f"unexpected response schema [ka20002]: {type(exc).__name__}") from exc
        return members

    # --- OrderPort (P5 Task 4 — 전 필드 G1/G2/G3 실측 확정, CLAUDE.md §5) ---

    async def get_quotes(self, symbols: list[str]) -> list[MarketData]:
        """ka10095 다종목 시세+호가. 100종목 초과는 내부에서 청크 분할(호출자
        무지 — 스펙 §5). 빈 행(결측/더미 종목)은 제외하고 반환한다."""
        for s in symbols:
            validate_symbol(s)  # 파이프 조인 전 형식 검증(스머글링 방어 — 보안 패널)
        results: list[MarketData] = []
        for i in range(0, len(symbols), _QUOTES_BATCH_MAX):
            chunk = symbols[i:i + _QUOTES_BATCH_MAX]
            data, _, _ = await self._client.call(
                "stkinfo", "ka10095", {"stk_cd": "|".join(chunk)})
            try:
                for row in data.get("atn_stk_infr") or []:
                    raw_symbol = row.get("stk_cd", "")
                    if not str(raw_symbol).strip():
                        continue  # 빈 행 — G1 실측: 더미 코드는 빈 행으로 옴
                    price = _to_price(row.get("cur_prc"))
                    bid = _to_price(row.get("buy_bid"))
                    ask = _to_price(row.get("sel_bid"))
                    if price <= 0:
                        # 참 degenerate(stk_cd는 있으나 가격 공백 — ka10081
                        # 012510과 동일 클래스) — price=0이 손절 판정으로 흐르면
                        # 위험. OrderPort 계약(결측 제외)에 따라 제외+경고.
                        logger.warning(
                            "ka10095 degenerate row for %s (price=0) — excluded",
                            raw_symbol)
                        continue
                    if bid <= 0 or ask <= 0:
                        # 호가 한쪽 0은 제외하지 **않는다**(broker-api 델타):
                        # 상한가는 매도호가(ask), 하한가는 매수호가(bid)가
                        # legit하게 소진될 수 있고, 그 순간이야말로 감시(손절/
                        # 트레일링 — price만 소비)가 가장 중요하다. 진입 지정가
                        # (ask 소비)는 갭가드가 상한가 후보를 이미 배제. 실제
                        # 상/하한가 ka10095 응답 형태는 PRE-GATE 실측 대상.
                        logger.info(
                            "ka10095 one-sided book for %s (bid=%d ask=%d) — kept",
                            raw_symbol, bid, ask)
                    results.append(MarketData(
                        quote=Quote(
                            symbol=_normalize_symbol(raw_symbol),
                            name=row.get("stk_nm", ""),
                            price=price,
                            change_rate=_to_decimal(row.get("flu_rt")),
                            volume=_to_int(row.get("trde_qty")),
                        ),
                        bid=bid,
                        ask=ask,
                    ))
            except (KeyError, ValueError, ArithmeticError, TypeError,
                    AttributeError) as exc:
                raise BrokerError(
                    f"unexpected response schema [ka10095]: {type(exc).__name__}"
                ) from exc
        return results

    async def place_order(self, req: OrderRequest) -> OrderAck:
        """kt10000(매수)/kt10001(매도). 응답 바디만 소비 — Authorization/토큰은
        어느 계층에도 없음(§9 보안). rc!=0(RC4003 호가단위 오류 등)은 client가
        ApiError로 표면화한다(fail-loud)."""
        data, _, _ = await self._client.call(
            _orders.CATEGORY_ORDER, _orders.order_api_id(req.side),
            _orders.build_order_body(req))
        try:
            return OrderAck(order_no=data["ord_no"],
                            message=data.get("return_msg", ""))
        except (KeyError, TypeError) as exc:
            raise BrokerError(
                f"unexpected response schema [order]: {type(exc).__name__}") from exc

    async def cancel_order(self, order_no: str, symbol: str) -> OrderAck:
        """kt10003 전량 취소(cncl_qty="0" — G2 실측 계약)."""
        validate_symbol(symbol)  # 실취소 경로 — 형식 오류는 호출 전 차단
        data, _, _ = await self._client.call(
            _orders.CATEGORY_ORDER, _orders.API_CANCEL,
            _orders.build_cancel_body(order_no, symbol))
        try:
            return OrderAck(order_no=data["ord_no"],
                            message=data.get("return_msg", ""))
        except (KeyError, TypeError) as exc:
            raise BrokerError(
                f"unexpected response schema [kt10003]: {type(exc).__name__}") from exc

    async def get_open_orders(self) -> list[OpenOrder]:
        """ka10075 미체결 조회. 단일 페이지만 관측됨(G2) — cont-yn=Y가 오면
        다중 페이지 미체결이 존재한다는 뜻이라 fail-loud(조용한 누락 방지;
        P5 운용상 미체결은 소수라 실질 발생 확률 낮음)."""
        data, cont, _ = await self._client.call(
            _orders.CATEGORY_ACCOUNT, _orders.API_OPEN_ORDERS,
            dict(_orders.OPEN_ORDERS_BODY))
        if cont == "Y":
            raise BrokerError(
                "ka10075 paginated response (cont-yn=Y) — unfilled orders "
                "exceed one page; refusing to silently truncate")
        try:
            return [_orders.parse_open_order(row, _normalize_symbol,
                                             _to_int, _to_price)
                    for row in data.get("oso") or []]
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise BrokerError(
                f"unexpected response schema [ka10075]: {type(exc).__name__}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()
