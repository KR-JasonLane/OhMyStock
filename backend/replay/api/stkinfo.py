"""ka10095 관심종목정보요청 재현(스펙 §7 — G1 실측,
`.superpowers/sdd/p5-pregate-G1.txt`).

실측 계약:
- 구분자는 **파이프만** 바인딩 — 세미콜론/콤마/공백 결합 문자열은 코드로
  인식되지 않아 **빈 1행**(미바인딩)이 돌아온다.
- `KRX:` 프리픽스도 실패(빈 행). 미지 코드는 요청 수만큼 행은 오되 해당
  행이 전부 빈 문자열(부분 실패 계약 — 호출자가 빈 stk_cd 행을 걸러야 함).
- 최대 100종목: 101+ → rc=5, 0행. 페이지네이션 없음(cont-yn 항상 'N').
- 행은 63필드(G1 원문 필드셋) — 엔진 소비분만 실값, 나머지는 형태 유지
  더미(§7: 잔량·그릭스 등은 비범위 명시).
- 호가 **합성**: sel_bid=현재가+1틱, buy_bid=현재가-1틱, 5단계는 ±1~5틱.
"""

import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from replay.api.common import (apply_api_fault, kiwoom_error, kiwoom_json,
                               prev_day_close, require_token, signed)
from replay.ticks import tick_size

router = APIRouter()

_CODE_RE = re.compile(r"^\d{6}$")
MAX_SYMBOLS = 100
OVER_LIMIT_RC = 5  # 실측: 101종목 → rc=5 (return_msg 원문은 미실측)

# G1 실측 63필드(첫 행 원문 순서) — 형태 대조 회귀의 1차 소스는 정제 픽스처
# (tests/replay_mock/fixtures/ka10095_row.json)이고, 이 목록은 렌더러 소유.
_FIELDS = (
    "stk_cd", "stk_nm", "cur_prc", "base_pric", "pred_pre", "pred_pre_sig",
    "flu_rt", "trde_qty", "trde_prica", "cntr_qty", "cntr_str",
    "pred_trde_qty_pre", "sel_bid", "buy_bid",
    "sel_1th_bid", "sel_2th_bid", "sel_3th_bid", "sel_4th_bid", "sel_5th_bid",
    "buy_1th_bid", "buy_2th_bid", "buy_3th_bid", "buy_4th_bid", "buy_5th_bid",
    "upl_pric", "lst_pric", "open_pric", "high_pric", "low_pric",
    "close_pric", "cntr_tm", "exp_cntr_pric", "exp_cntr_qty", "cap", "fav",
    "mac", "stkcnt", "bid_tm", "dt", "pri_sel_req", "pri_buy_req",
    "pri_sel_cnt", "pri_buy_cnt", "tot_sel_req", "tot_buy_req", "tot_sel_cnt",
    "tot_buy_cnt", "prty", "gear", "pl_qutr", "cap_support", "elwexec_pric",
    "cnvt_rt", "elwexpr_dt", "cntr_engg", "cntr_pred_pre", "theory_pric",
    "innr_vltl", "delta", "gam", "theta", "vega", "law",
)


def _empty_row() -> dict:
    """미바인딩/미지 코드 행 — 실측: 전 필드 빈 문자열(stk_cd 포함)."""
    return {name: "" for name in _FIELDS}


def _market_of(request: Request, code: str) -> str:
    settings = request.app.state.settings
    return "etf" if code in settings.etf_symbols else settings.default_market


def _row(request: Request, code: str) -> dict:
    if request.app.state.faults.is_halted(code):
        # §9 거래정지 — 모니터가 "시세 결측 지속"을 관측하도록 빈 행
        return _empty_row()
    candle = request.app.state.store.last_at_or_before(
        code, request.app.state.clock.now())
    if candle is None:
        return _empty_row()
    price = candle.close
    tick = tick_size(price, _market_of(request, code))
    now = request.app.state.clock.now()
    # 전일 종가 대비 실값(스펙 §7 — flu_rt는 "엔진 소비분 실값" 목록.
    # broker-api R4: 상수 0%는 등락률 소비 로직의 버그를 통과시킨다).
    # 선적재 구간에 전일이 없으면 보합 폴백.
    prev = prev_day_close(request.app.state.store, code, now) or price
    change = price - prev
    change_sig = "3" if change == 0 else ("2" if change > 0 else "5")
    row = _empty_row()
    row.update({
        "stk_cd": code,
        "stk_nm": code,       # 목 단순화 — 종목명 미보유(파서는 표시용만)
        "cur_prc": signed(price),
        "base_pric": str(prev),         # 기준가=전일종가, 실측 표기: 부호 없음
        "pred_pre": signed(change),
        "pred_pre_sig": change_sig,
        "flu_rt": f"{change / prev * 100:+.2f}" if prev else "+0.00",
        "trde_qty": str(candle.volume),
        "trde_prica": "0", "cntr_qty": "+0", "cntr_str": "0.00",
        "pred_trde_qty_pre": "0.00",
        "sel_bid": signed(price + tick),
        "buy_bid": signed(price - tick),
        "open_pric": signed(candle.open),
        "high_pric": signed(candle.high),
        "low_pric": signed(candle.low),
        "close_pric": signed(price),
        "cntr_tm": now.strftime("%H%M%S"),
        "bid_tm": now.strftime("%H%M%S"),
        "dt": now.strftime("%Y%m%d"),
        "exp_cntr_pric": signed(price), "exp_cntr_qty": "0",
        "upl_pric": signed(round(price * 1.3)),
        "lst_pric": signed(-round(price * 0.3)),  # 실측 표기: 음수 문자열
        "cap": "0", "fav": "100", "mac": "0", "stkcnt": "0",
        "pri_sel_req": "0", "pri_buy_req": "0",
        "tot_sel_req": "0", "tot_buy_req": "0", "tot_sel_cnt": "0",
        "prty": "0.00", "gear": "0.00", "pl_qutr": "0.00",
        "cap_support": "0.00", "elwexec_pric": "0", "cnvt_rt": "0.0000",
        "elwexpr_dt": "00000000",
    })
    # 5단계 합성 호가(±1~5틱 — 잔량은 비범위, 빈 문자열 유지)
    for level in range(1, 6):
        row[f"sel_{level}th_bid"] = signed(price + level * tick)
        row[f"buy_{level}th_bid"] = signed(price - level * tick)
    return row


@router.post("/api/dostk/stkinfo")
async def stkinfo(request: Request) -> JSONResponse:
    api_id = request.headers.get("api-id", "")
    fault = await apply_api_fault(request, api_id)
    if fault is not None:
        return fault
    denied = require_token(request)
    if denied is not None:
        return denied
    if api_id != "ka10095":
        return kiwoom_error(1, f"[REPLAY] unsupported TR {api_id!r} "
                               "(stkinfo category: ka10095 only — spec §7)")
    # 조회 TR 진입 계약(플랜 R4): 응답 전에 체결 판정 1회 — 호출 시점이
    # 배선마다 다르면 동일 시나리오의 체결 시점이 재현 불가능해진다.
    request.app.state.engine.check_fills()
    body = await request.json()
    raw = str(body.get("stk_cd", ""))
    parts = [p for p in raw.split("|") if p != ""]
    if len(parts) > MAX_SYMBOLS:
        return kiwoom_error(OVER_LIMIT_RC,
                            "조회 종목 수 상한(100) 초과")  # msg 원문 미실측
    rows = []
    for part in parts:
        if not _CODE_RE.match(part):
            # 비파이프 구분자 결합('005930;000660')·KRX: 프리픽스 — 실측:
            # 미바인딩 빈 행 1개
            rows.append(_empty_row())
        else:
            rows.append(_row(request, part))
    return kiwoom_json({"atn_stk_infr": rows})
