"""계좌 TR 재현(스펙 §7 — G2/G3 실측, `.superpowers/sdd/p5-pregate-G{2,3}.txt`).

실측 계약:
- ka10075(미체결): 리스트 키 **`oso`**, 행 30필드. `ord_pric`은 **패딩 없는**
  정수 문자열('246000'), `ord_stt`="접수", `io_tp_nm`은 **접두 부분문자열**
  ('-매도' — 정확일치 파서를 깨뜨리는 실측 형태 유지. 매수 값은 미실측 —
  부호 관례 대칭('+매수')으로 근사, containment 파서 전제라 어느 쪽이든
  안전). 전파 지연(§8): visible_after 이전 주문은 미노출.
- kt00001(예수금): 최상위 `entr`/`ord_alow_amt`. `ord_alow_amt`는 미체결
  매수 예약 차감 후 금액(실서버 의미론 — Account.reserved_buy_total 공유).
  패딩 폭은 미실측 — kt00018 관례(15자리) 준용.
- kt00018(잔고): 최상위 `tot_evlt_amt`/`tot_evlt_pl` **필수**(포지션 0건
  이어도 존재 — 어댑터 하드 인덱싱 계약), 리스트 키
  `acnt_evlt_remn_indv_tot`, 행 23필드. `stk_cd`는 **`A` 프리픽스**, 전
  필드 제로패딩 — 폭은 G3 원문 실측(cur_prc/pred_close_pric 12, 비율 12,
  나머지 15). 음수는 부호가 폭에 포함('-00000000002694').
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from replay.account import Holding, commission, eval_holdings, sell_tax
from replay.api.common import (apply_api_fault, kiwoom_error, kiwoom_json,
                               pad_dec, pad_int, prev_day_close,
                               require_token, signed)
from replay.ticks import tick_size

router = APIRouter()

QUERY_OK_MSG = "모의투자 조회완료"  # G3 실측 문구
_W15 = 15   # kt00018 기본 패딩 폭(실측)
_W12 = 12   # cur_prc/pred_close_pric/비율 폭(실측)


def _ka10075(request: Request) -> JSONResponse:
    account = request.app.state.account
    settings = request.app.state.settings
    price_now = request.app.state.engine.price_now
    rows = []
    for order in account.visible_open_orders(request.app.state.wall_now()):
        price = price_now(order.symbol) or 0
        # 호가 합성은 ka10095와 동일 규칙(±1틱 — 트레이더 R4 Minor 정렬)
        market = ("etf" if order.symbol in settings.etf_symbols
                  else settings.default_market)
        tick = tick_size(price, market) if price > 0 else 0
        label = "+매수" if order.side == "buy" else "-매도"
        rows.append({
            "ord_no": order.order_no,
            "stk_cd": order.symbol,          # 실측: ka10075는 A 프리픽스 없음
            "stk_nm": order.symbol,
            "ord_qty": str(order.quantity),
            "oso_qty": str(order.unfilled),
            "ord_pric": str(order.price),    # 실측: 패딩 없는 표기
            "ord_stt": "접수",
            "orig_ord_no": "0000000",
            "io_tp_nm": label,               # 접두 부분문자열(실측 '-매도')
            "trde_tp": "0" if order.style == "limit" else "3",
            "tm": order.submitted_at.strftime("%H%M%S"),
            "cntr_qty": str(order.quantity - order.unfilled),
            "cntr_no": "", "cntr_pric": "0", "cntr_tot_amt": "0",
            "cur_prc": signed(price),
            "sel_bid": signed(price + tick), "buy_bid": signed(price - tick),
            "stex_tp": "1", "stex_tp_txt": "KRX",
            "acnt_no": "", "ind_invsr": "", "mang_empno": "",
            "sor_yn": "N", "stop_pric": "0",
            "tdy_trde_cmsn": "0", "tdy_trde_tax": "0",
            "tsk_tp": "", "unit_cntr_pric": "0", "unit_cntr_qty": "0",
        })
    return kiwoom_json({"oso": rows, "return_msg": QUERY_OK_MSG})


def _kt00001(request: Request) -> JSONResponse:
    account = request.app.state.account
    available = account.cash - account.reserved_buy_total()
    return kiwoom_json({
        "entr": pad_int(account.cash, _W15),
        "ord_alow_amt": pad_int(max(available, 0), _W15),
        "return_msg": QUERY_OK_MSG,
    })


def _pending_sell_qty(account) -> dict[str, int]:
    pending: dict[str, int] = {}
    for order in account.open_orders.values():
        if order.side == "sell" and order.unfilled > 0:
            pending[order.symbol] = pending.get(order.symbol, 0) + order.unfilled
    return pending


def _holding_row(holding, price: int, prev_close: int,
                 sellable: int) -> dict:
    """kt00018 개별 포지션 행(G3 실측 23필드 — 폭 15/12, A 프리픽스).
    poss_rt는 실측 샘플(단일 보유 100%)의 고정 재현 — 다보유 시 실서버
    산식은 미실측(비소비 필드)."""
    eval_amt = price * holding.quantity
    profit = (price - holding.avg_price) * holding.quantity
    rate = (profit / holding.total_cost * 100) if holding.total_cost else 0.0
    buy_fee = commission(holding.total_cost)
    sell_fee = commission(eval_amt)
    return {
        "stk_cd": f"A{holding.symbol}",   # 실측: A 프리픽스
        "stk_nm": holding.symbol,
        "evltv_prft": pad_int(profit, _W15),
        "prft_rt": pad_dec(rate, _W12),
        "pur_pric": pad_int(holding.avg_price, _W15),
        "pred_close_pric": pad_int(prev_close, _W12),  # 전일 종가 실값
        "rmnd_qty": pad_int(holding.quantity, _W15),
        "trde_able_qty": pad_int(sellable, _W15),
        "cur_prc": pad_int(price, _W12),
        "pred_buyq": pad_int(0, _W15),
        "pred_sellq": pad_int(0, _W15),
        "tdy_buyq": pad_int(0, _W15),
        "tdy_sellq": pad_int(0, _W15),
        "pur_amt": pad_int(holding.total_cost, _W15),
        "pur_cmsn": pad_int(buy_fee, _W15),
        "evlt_amt": pad_int(eval_amt, _W15),
        "sell_cmsn": pad_int(sell_fee, _W15),
        "tax": pad_int(sell_tax(eval_amt, holding.market), _W15),
        "sum_cmsn": pad_int(buy_fee + sell_fee, _W15),
        "poss_rt": pad_dec(100.0, _W12),
        "crd_tp": "00", "crd_tp_nm": "", "crd_loan_dt": "",
    }


def _kt00018(request: Request) -> JSONResponse:
    account = request.app.state.account
    price_now = request.app.state.engine.price_now
    snapshot = request.app.state.faults.balance_snapshot()
    if snapshot is not None:
        # §9 잔고 반영 지연 — 동결 창 동안 활성화 시점 스냅샷을 렌더링
        # (창 내 체결이 잔고에 안 보임 → 유령 판정 2회 확인 방어선 검증)
        holdings = {s: Holding(s, m, q, c)
                    for s, (m, q, c) in snapshot["holdings"].items()}
    else:
        holdings = account.holdings
    prices = {}
    for symbol in holdings:
        price = price_now(symbol)
        if price is not None:
            prices[symbol] = price
    if snapshot is None:
        # 실계좌 뷰 — Account.eval_total(결측 카운터 표면화 포함)
        total_eval, total_profit = account.eval_total(prices)
    else:
        # 동결 뷰 — 같은 수식(eval_holdings 공유), 카운터는 실시간 뷰 전용
        total_eval, total_profit, _ = eval_holdings(holdings, prices)
    total_purchase = sum(h.total_cost for h in holdings.values())
    pending_sells = _pending_sell_qty(account)
    now = request.app.state.clock.now()
    store = request.app.state.store
    rows = []
    for holding in holdings.values():
        price = prices.get(holding.symbol, holding.avg_price)
        rows.append(_holding_row(
            holding, price,
            prev_day_close(store, holding.symbol, now) or price,
            holding.quantity - pending_sells.get(holding.symbol, 0)))
    cash = snapshot["cash"] if snapshot is not None else account.cash
    return kiwoom_json({
        # 최상위 필수(실측 — 포지션 0건이어도 존재해야 어댑터가 살아남는다)
        "tot_evlt_amt": pad_int(total_eval, _W15),
        "tot_evlt_pl": pad_int(total_profit, _W15),
        "tot_pur_amt": pad_int(total_purchase, _W15),
        "tot_prft_rt": pad_dec(
            (total_profit / total_purchase * 100) if total_purchase else 0.0,
            _W12),
        "prsm_dpst_aset_amt": pad_int(cash + total_eval, _W15),
        "tot_crd_loan_amt": pad_int(0, _W15),
        "tot_crd_ls_amt": pad_int(0, _W15),
        "tot_loan_amt": pad_int(0, _W15),
        "acnt_evlt_remn_indv_tot": rows,
        "return_msg": QUERY_OK_MSG,
    })


@router.post("/api/dostk/acnt")
async def acnt(request: Request) -> JSONResponse:
    api_id = request.headers.get("api-id", "")
    fault = await apply_api_fault(request, api_id)
    if fault is not None:
        return fault
    denied = require_token(request)
    if denied is not None:
        return denied
    handlers = {"ka10075": _ka10075, "kt00001": _kt00001,
                "kt00018": _kt00018}
    handler = handlers.get(api_id)
    if handler is None:
        return kiwoom_error(1, f"[REPLAY] unsupported TR {api_id!r} "
                               "(acnt category: ka10075/kt00001/kt00018)")
    # 조회 TR 진입 계약(플랜 R4) — 응답 전에 체결 판정 1회
    request.app.state.engine.check_fills()
    await request.body()   # 바디 소비(형태 검증은 비범위 — G2 관용 실측)
    return handler(request)
