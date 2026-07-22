"""주문 TR 재현(스펙 §7 — G2/G3 실측, `.superpowers/sdd/p5-pregate-G2.txt`).

실측 계약:
- kt10000(매수)/kt10001(매도): body `{dmst_stex_tp:"KRX", stk_cd, ord_qty,
  trde_tp("0"=지정가|"3"=시장가 — **한 자리**), ord_uv(지정가만)}`.
  계좌번호/비밀번호 필드 없음(앱키에 계좌 바인딩). 응답 `{ord_no,
  dmst_stex_tp, return_code, return_msg}` — "모의투자 매수/매도주문완료".
- 지정가 틱 위반 → rc=20 + `[2000](RC4003:모의투자 호가단위 오류입니다.)`
  (tick-probe 실측). 기타 거부(예수금 부족 등)의 실서버 rc는 미실측 —
  rc=20으로 통일하고 사유를 return_msg에 노출(§7 관용).
- kt10003(취소): body `{dmst_stex_tp, orig_ord_no, stk_cd, cncl_qty:"0"}`
  (**"0"=전량취소** — 부분취소는 미실측·미지원). 응답 `{ord_no,
  base_orig_ord_no, cncl_qty, ...}` "모의투자 취소주문완료".
- 시장가에 ord_uv 동봉은 거부(스펙 §7 "ord_uv 부재 검증" — 실서버 동작
  미실측, 스펙 지시에 따른 보수적 검증).
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from replay.api.common import (apply_api_fault, kiwoom_error, kiwoom_json,
                               require_token)

router = APIRouter()

REJECT_RC = 20  # RC4003 실측 rc — 기타 거부도 통일(개별 rc 미실측)


def _market_override(request: Request, code: str) -> str | None:
    """ETF만 명시 전달, 그 외 None — 엔진의 시장 결정(매도는 보유 포지션
    ground truth, 신규 매수는 default_market)에 위임한다. ⚠️ stkinfo의
    _market_of(기본시장 폴백)와 시맨틱이 다르다(개발자 R4 Minor — 통합
    금지: 여기서 기본값을 채우면 엔진의 보유 시장 우선 규칙을 가린다)."""
    settings = request.app.state.settings
    return "etf" if code in settings.etf_symbols else None


def _submit(request: Request, body: dict, side: str) -> JSONResponse:
    engine = request.app.state.engine
    symbol = str(body.get("stk_cd", "")).strip()
    trde_tp = str(body.get("trde_tp", ""))
    ord_uv = str(body.get("ord_uv", "") or "")
    if not symbol:
        return kiwoom_error(REJECT_RC, "[REPLAY] stk_cd is required")
    try:
        quantity = int(body.get("ord_qty", ""))
    except ValueError:
        return kiwoom_error(REJECT_RC, "[REPLAY] invalid ord_qty")
    if trde_tp == "0":
        style = "limit"
        try:
            limit_price = int(ord_uv)
        except ValueError:
            return kiwoom_error(REJECT_RC, "[REPLAY] limit requires ord_uv")
    elif trde_tp == "3":
        style = "market"
        limit_price = 0
        if ord_uv:
            # 스펙 §7 보수적 검증(실서버 동작 미실측) — 시장가+가격 동봉은
            # 호출측 버그 신호이므로 조용히 무시하지 않는다
            return kiwoom_error(REJECT_RC,
                                "[REPLAY] market order must omit ord_uv")
    else:
        # 실측: 유효값은 한 자리 "0"/"3" — 두 자리("00"/"03") 문서는 오류
        return kiwoom_error(REJECT_RC,
                            f"[REPLAY] invalid trde_tp {trde_tp!r} "
                            "(single-digit '0'|'3' — measured)")
    result = engine.submit(symbol, side, style, quantity, limit_price,
                           market=_market_override(request, symbol))
    if not result.ok:
        return kiwoom_error(REJECT_RC, result.reason)
    label = "매수" if side == "buy" else "매도"
    return kiwoom_json({
        "ord_no": result.order_no,
        "dmst_stex_tp": str(body.get("dmst_stex_tp", "KRX")),
        "return_msg": f"모의투자 {label}주문완료",
    })


def _cancel(request: Request, body: dict) -> JSONResponse:
    engine = request.app.state.engine
    account = request.app.state.account
    orig = str(body.get("orig_ord_no", "")).strip()
    cncl_qty = str(body.get("cncl_qty", ""))
    if cncl_qty != "0":
        # 실측은 "0"=전량뿐 — 부분취소 요청은 미지원을 fail-loud로 알린다
        return kiwoom_error(REJECT_RC,
                            "[REPLAY] only cncl_qty='0' (전량취소) supported")
    order = account.open_orders.get(orig)
    cancelled_qty = order.unfilled if order else 0
    result = engine.cancel(orig)
    if not result.ok:
        return kiwoom_error(REJECT_RC, result.reason)
    return kiwoom_json({
        # 취소 주문 자체의 신규 번호 — 응답 **키**만 실측(G2), 값 의미(신규
        # 채번 vs 원번호 재사용)는 미실측 가정(broker-api R4 Minor 라벨).
        # 프로덕션 cancel_order()는 존재만 확인하므로 파서 영향 없음.
        "ord_no": account.next_order_no(),
        "base_orig_ord_no": orig,
        "cncl_qty": str(cancelled_qty),
        "return_msg": "모의투자 취소주문완료",
    })


@router.post("/api/dostk/ordr")
async def ordr(request: Request) -> JSONResponse:
    api_id = request.headers.get("api-id", "")
    fault = await apply_api_fault(request, api_id)
    if fault is not None:
        return fault
    denied = require_token(request)
    if denied is not None:
        return denied
    # 주문 TR도 진입 시 체결 판정 1회(아키텍트 R4 — 조회 TR만 갱신하면
    # 조회 없는 연속 제출에서 이미 크로스된 미체결의 예약금·보유량이 낡은
    # 스냅샷으로 남아 실서버라면 통과할 주문을 오거부한다. 예: 체결됐어야
    # 할 매수 후 즉시 매도 → "insufficient holdings" 오거부).
    request.app.state.engine.check_fills()
    body = await request.json()
    # if-체인 디스패치: acnt의 dict 디스패치와 달리 kt10000/kt10001이 같은
    # 핸들러에 side 인자 바인딩을 요구해 균일 시그니처가 안 나온다(의도)
    if api_id == "kt10000":
        return _submit(request, body, "buy")
    if api_id == "kt10001":
        return _submit(request, body, "sell")
    if api_id == "kt10003":
        return _cancel(request, body)
    return kiwoom_error(1, f"[REPLAY] unsupported TR {api_id!r} "
                           "(ordr category: kt10000/kt10001/kt10003)")
