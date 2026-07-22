"""Phase 5 PRE-GATE G2 실측 — 주문 TR (kt10000 매수 / kt10003 취소 / ka10075 미체결).

실행(모의서버, 장중 09:00~15:30 필수):
  cd backend && PYTHONPATH=. .venv/bin/python scripts/pregate_g2_orders.py > ../.superpowers/sdd/p5-pregate-G2.txt 2>&1

⚠️ 실제 주문을 발행한다(모의계좌). 안전 설계(이중):
  1. 체결 안 되는 낮은 매수 지정가(현재가 -10%, 밴드 안)로 1주 → 미체결 확보
  2. **취소 대상 ord_no를 매수 응답이 아니라 ka10075(미체결 조회)에서 종목+수량+
     가격 매칭으로 확보** — 응답 필드명이 불확실해도 실제 주문을 찾아 취소한다
  3. 취소 필드명 2후보 fallback(orig_ord_no/cncl_qty, org_ord_no/ord_qty)
  4. finally에서 미취소분을 ka10075 재조회로 확인 + 큰 경고
실제 체결·포지션 없음. (G3 실포지션은 별도.)

확인 목표(계획서 Task 0 / 스펙 §4 G2):
  1. kt10000 매수 바디·trde_tp 코드값(공식 추정 "0" vs 스펙 "00" 실측)
  2. 응답 주문번호 필드명(ord_no 등) — 실측 확정
  3. ka10075 미체결 응답(리스트 키 oso, 행 필드)
  4. kt10003 취소 필드명·cncl_qty="0" 전량 동작
  5. 레이트리밋 버킷 분리 — 주문(ordr) 직후 시세(stkinfo) 429 여부

명세 근거: **커뮤니티 래퍼 조사(비공식, 상호 불일치 확인됨) — 실측으로 확정.**
공식 openapi.kiwoom.com은 JS 렌더라 크롤 불가. 커뮤니티 소스는 취소 필드명
(orig_ord_no vs org_ord_no)·dmst_stex_tp("KRX" vs "01")에서 서로 엇갈림 →
전부 실측 대상. 관측된 category는 stkinfo/chart/acnt뿐, ordr은 미관측.

전제: .env 모의 키(KIWOOM_MOCK=true), 예수금 존재. 백엔드 동시 가동 금지(1토큰).
"""

import asyncio

import httpx

from app.adapters.kiwoom.auth import TokenManager
from app.adapters.kiwoom.broker import KiwoomBroker
from app.adapters.kiwoom.client import KiwoomHttpClient
from app.adapters.kiwoom.rate_limiter import RateLimiter
from app.core.config import Settings
from app.domain.errors import BrokerError

MOCK_BASE = "https://mockapi.kiwoom.com"
PROBE = "005930"  # 삼성전자 — 초유동, 체결 불가 지정가 매수에 안전
ORD_QTY = "1"

# trde_tp(주문유형) 후보: 공식 추정 단일자리, 스펙 문서는 두자리 — 실측 확정
TRDE_TP_LIMIT_CANDIDATES = ("0", "00")  # 지정가

# 취소 필드명 후보(커뮤니티 소스 상충) — (원주문번호 키, 수량 키, 수량값)
CANCEL_FIELD_CANDIDATES = (
    ("orig_ord_no", "cncl_qty"),
    ("org_ord_no", "ord_qty"),
)


async def _call(client: KiwoomHttpClient, category: str, api_id: str, body: dict):
    """(body, rc, msg, cont_yn) 반환. BrokerError 흡수."""
    try:
        resp, cont, _next = await client.call(category, api_id, body)
        return resp, str(resp.get("return_code")), resp.get("return_msg"), cont
    except BrokerError as exc:
        return {}, str(getattr(exc, "return_code", "?")), \
            getattr(exc, "return_msg", None) or str(exc), "N"


async def _fetch_open_orders(client: KiwoomHttpClient) -> tuple[list, dict, str, str]:
    """ka10075 미체결 조회. (oso 리스트, 원응답, rc, msg) 반환.
    rc는 호출부가 '조회 실패'와 '미체결 0건'을 구분하는 데 쓴다(broker-api 지적)."""
    body = {"all_stk_tp": "0", "trde_tp": "0", "stex_tp": "0"}
    resp, rc, msg, cont = await _call(client, "acnt", "ka10075", body)
    if cont == "Y":
        print("  ⚠️ ka10075 cont-yn=Y — 미체결이 다중 페이지(이 스크립트는 1페이지만 봄)")
    oso = resp.get("oso")
    return (oso if isinstance(oso, list) else []), resp, rc, msg


def _find_my_orders(oso: list, safe_px: int) -> list[dict]:
    """미체결 리스트에서 방금 낸 주문 후보(종목+수량)를 **전부** 수집.
    가격까지 일치하면 우선. 자동 취소는 호출부가 '후보 정확히 1건'일 때만 하도록
    책임을 넘긴다(dev 지적 — 다중 후보 첫번째 자동취소는 재실행 오탐 위험)."""
    by_sym_qty = [r for r in oso
                  if r.get("stk_cd", "").endswith(PROBE)
                  and str(r.get("ord_qty", "")).lstrip("0+-") == ORD_QTY]
    priced = [r for r in by_sym_qty
              if (str(r.get("ord_pric", "")).lstrip("+-").lstrip("0") or "0") == str(safe_px)]
    # 가격까지 맞는 후보가 있으면 그것들, 없으면 종목+수량 후보 전체(모호성은 호출부가 판단)
    return priced or by_sym_qty


async def _try_cancel(client: KiwoomHttpClient, ord_no: str) -> bool:
    """취소 필드명 2후보를 순차 시도. 성공(rc=0) 시 True."""
    for ord_key, qty_key in CANCEL_FIELD_CANDIDATES:
        body = {"dmst_stex_tp": "KRX", ord_key: ord_no, "stk_cd": PROBE, qty_key: "0"}
        resp, rc, msg, _ = await _call(client, "ordr", "kt10003", body)
        print(f"  취소 시도 [{ord_key}/{qty_key}] → rc={rc} msg={msg!r} "
              f"resp_keys={sorted(resp.keys())}")
        if rc == "0":
            print(f"  ✅ 취소 성공 (필드명 확정: {ord_key}/{qty_key})")
            return True
    return False


async def main() -> None:
    settings = Settings()
    if not settings.kiwoom_mock:
        print("SKIP: KIWOOM_MOCK=true 아님 — 모의서버 전용")
        return

    async with httpx.AsyncClient(base_url=MOCK_BASE, timeout=15) as http:
        tm: TokenManager | None = None
        client: KiwoomHttpClient | None = None
        order_accepted = False   # rc=0 여부(ord_no 파싱과 독립)
        cancelled = False
        safe_px = 0
        try:
            limiter = RateLimiter()
            tm = TokenManager(http, settings.kiwoom_app_key.get_secret_value(),
                              settings.kiwoom_secret_key.get_secret_value(), limiter=limiter)
            client = KiwoomHttpClient(settings, http=http, token_manager=tm, limiter=limiter)
            broker = KiwoomBroker(client)
            await tm.get_token()

            # [사전] 예수금 + 현재가(안전 지정가 계산)
            print("=== [사전] 예수금 / 현재가 ===")
            deposit = await broker.get_deposit()
            print(f"예수금 available={deposit.available:,} total={deposit.total:,}")
            quote = await broker.get_quote(PROBE)
            cur = quote.price
            # 현재가 -10%, 1000원 단위 내림(삼성전자 20만+대 호가단위) — 체결 불가 + 밴드 안
            safe_px = max(1000, int(cur * 0.9) // 1000 * 1000)
            print(f"{PROBE} 현재가={cur:,} → 안전 매수 지정가(미체결·밴드안)={safe_px:,}")

            # [1] 매수 주문 kt10000 — trde_tp 후보 순차
            print("\n=== [1] 매수 주문 kt10000 (1주, 체결불가 지정가) ===")
            for tp in TRDE_TP_LIMIT_CANDIDATES:
                body = {"dmst_stex_tp": "KRX", "stk_cd": PROBE, "ord_qty": ORD_QTY,
                        "trde_tp": tp, "ord_uv": str(safe_px)}
                resp, rc, msg, _ = await _call(client, "ordr", "kt10000", body)
                print(f"  trde_tp={tp!r} → rc={rc} msg={msg!r} "
                      f"resp_keys={sorted(resp.keys())} ord_no={resp.get('ord_no')!r}")
                if rc == "0":
                    order_accepted = True
                    print(f"  ✅ 주문 접수 — trde_tp={tp!r} 유효. 전체 응답: {resp}")
                    break
                # 가격 밴드/틱 거부인지 trde_tp 문제인지 msg로 구분 힌트
            if not order_accepted:
                print("  ⚠️ 모든 trde_tp 후보 rc!=0 — 가격 거부(밴드/틱) vs trde_tp 미지원, msg 참고")

            # [2] 레이트리밋 버킷 — 주문 직후 시세 429 여부
            print("\n=== [2] 레이트리밋 버킷 분리 (주문 직후 시세) ===")
            resp, rc, msg, _ = await _call(client, "stkinfo", "ka10095", {"stk_cd": PROBE})
            print(f"  주문 직후 ka10095 → rc={rc} (429 없이 rc=0이면 주문/시세 버킷 분리 정황)")

            # [3] 미체결 조회 ka10075 — 응답 구조 + 내 주문 ord_no 확보
            print("\n=== [3] 미체결 조회 ka10075 (acnt) — ord_no 확보 ===")
            oso, resp, rc, msg = await _fetch_open_orders(client)
            if rc != "0":
                print(f"  ‼️ ka10075 조회 실패 rc={rc} msg={msg!r} — stex_tp='0' 값 문제 가능"
                      f"(dmst_stex_tp처럼 'KRX' 리터럴 필요할 수 있음)")
            print(f"  응답 키={sorted(resp.keys())}  미체결 {len(oso)}건")
            my_ord_no: str | None = None
            if oso:
                print(f"  행 필드: {sorted(oso[0].keys())}")
                cands = _find_my_orders(oso, safe_px)
                if len(cands) == 1:
                    mine = cands[0]
                    my_ord_no = mine.get("ord_no")
                    print(f"  ✅ 내 주문 매칭(후보 1건): ord_no={my_ord_no!r} stk_cd={mine.get('stk_cd')!r} "
                          f"ord_qty={mine.get('ord_qty')!r} oso_qty={mine.get('oso_qty')!r} "
                          f"ord_pric={mine.get('ord_pric')!r} ord_stt={mine.get('ord_stt')!r}")
                elif len(cands) > 1:
                    print(f"  ‼️ 매칭 후보 {len(cands)}건 — 자동 취소 생략(오탐 방지, dev 지적). 수동 확인:")
                    for r in cands:
                        print(f"      ord_no={r.get('ord_no')!r} pric={r.get('ord_pric')!r} tm={r.get('tm') or r.get('ord_tm')!r}")

            # [4] 취소 kt10003 — 후보 정확히 1건일 때만 자동 취소(필드명 2후보 fallback)
            print("\n=== [4] 취소 kt10003 (cncl_qty=0 전량, 필드명 fallback) ===")
            if my_ord_no:
                cancelled = await _try_cancel(client, my_ord_no)
                if not cancelled:
                    print("  ‼️ 두 필드명 후보 모두 취소 실패 — finally에서 재확인")
            elif order_accepted:
                print("  ‼️ 주문 접수(rc=0)됐으나 ord_no 단일 확정 실패 — finally 재확인/수동 확인 필요")
            else:
                print("  (접수된 주문 없음 — 취소 생략)")

            print("\n=== G2 판정 체크리스트 ===")
            print(f"  [{'x' if order_accepted else ' '}] 매수 주문 접수(rc=0)?")
            print(f"  [{'x' if my_ord_no else ' '}] ka10075에서 ord_no 단일 확보?")
            print(f"  [{'x' if cancelled else ' '}] 취소 성공?")
            print("  [ ] trde_tp 유효값 / ord_no 필드명 / oso 행 필드 — 위 로그에서 판독")
            print("  [ ] ka10075 stex_tp='0' 유효? 아니면 'KRX' 리터럴 필요? ([3] rc)")
            print("  [ ] 주문/시세 레이트리밋 버킷 분리? ([2] 429 여부)")
            print("  [ ] 계좌번호 필드 없이 동작? (바디 무계좌로 rc=0)")
        finally:
            # 안전망: 취소 확정 전이면(플래그 무관) ka10075 재조회로 미취소분 확인.
            # 게이트를 order_accepted가 아닌 not cancelled로 — 비-BrokerError 예외로
            # order_accepted가 안 세팅된 경우도 커버(dev 지적, 조회는 읽기전용이라 저비용).
            if client is not None and not cancelled:
                print("\n[안전정리] 취소 미확정 — ka10075 재조회로 미취소분 확인")
                try:
                    oso, _resp, rc, msg = await _fetch_open_orders(client)
                    if rc != "0":
                        print(f"  ‼️‼️ 안전정리 조회 실패 rc={rc} msg={msg!r} — 수동으로 미체결 확인·취소 필요")
                    else:
                        cands = _find_my_orders(oso, safe_px) if oso else []
                        if len(cands) == 1 and cands[0].get("ord_no"):
                            ok = await _try_cancel(client, cands[0]["ord_no"])
                            if not ok:
                                print(f"  ‼️‼️ 안전정리 취소 실패 — 수동 취소 필요: ord_no={cands[0].get('ord_no')!r}")
                        elif not cands:
                            # 미체결 없음 — 체결로 사라졌을 가능성까지 잔고로 방어 확인
                            try:
                                bal = await broker.get_balance()
                                held = [p for p in bal.positions if p.symbol.endswith(PROBE)]
                                print(f"  미체결 없음 — 신규 포지션 확인: {held or '없음(안전)'}")
                            except Exception as be:
                                print(f"  미체결 없음(잔고 확인 실패: {type(be).__name__}) — 수동 확인 권장")
                        else:
                            print(f"  ‼️ 미체결 후보 {len(cands)}건 — 자동 취소 생략, 수동 확인 필요:")
                            for r in cands:
                                print(f"      ord_no={r.get('ord_no')!r} pric={r.get('ord_pric')!r}")
                except Exception as e:
                    print(f"  ‼️‼️ 안전정리 예외: {type(e).__name__}: {e} — 수동 확인 필요")
            if tm is not None:
                await tm.revoke()
                print("[토큰 revoke 완료]")


if __name__ == "__main__":
    asyncio.run(main())
