"""Phase 5 PRE-GATE G3 실측 — kt00018 잔고 행 단위 필드 (기존 PRE-GATE #1).

실행(모의서버, 장중 09:00~15:30 필수):
  cd backend && PYTHONPATH=. .venv/bin/python scripts/pregate_g3_balance.py > ../.superpowers/sdd/p5-pregate-G3.txt 2>&1

⚠️ 실제 체결(포지션 생성)을 발생시킨다(모의계좌). 안전 설계:
  1. 1주만 매수 → 잔고 행 확인 → **즉시 시장가 매도로 청산**
  2. 청산 판단은 "매도 rc=0 + 보수적 확인" — 확인이 실패/불명이면 미청산으로 간주
  3. finally에서 order_accepted면 cleared 확정 전까지 폴링+재매도
  4. 청산 확인은 raw kt00018 기반(broker.get_balance 파싱 독립) + 필드명 후보/알려진 키
실제 체결 후 반드시 청산. 잔존 시 큰 경고로 수동 청산 유도.

확인 목표(스펙 §4 G3 / CLAUDE.md §5 PRE-GATE #1):
  1. kt00018 리스트 키(acnt_evlt_remn_indv_tot 추정)와 행 단위 필드
  2. **avg_price/pur_pric가 원 단위 정수인가**(TradePosition.avg_price:int 가정 검증)
  3. broker.get_balance() 파싱이 실제 행과 일치하는가(행 수 대조)
  4. trde_tp="3"(시장가) 유효성(매수/매도) — G2는 "0"(지정가·매수만) 확인

명세 근거(G2 실측 확정): category="ordr", 매수 kt10000/매도 kt10001,
바디 {dmst_stex_tp="KRX", stk_cd, ord_qty, trde_tp, ord_uv(지정가만)}, 응답 ord_no,
계좌필드 없음. 잔고 kt00018 category="acnt", 바디 qry_tp="1"/dmst_stex_tp="KRX".
⚠️ 매도(kt10001) 바디·trde_tp="3"·kt00018 행 필드는 미실측 → 이 G3가 확정.

전제: .env 모의 키(KIWOOM_MOCK=true), 예수금 충분(1주≈27만원). 백엔드 동시 가동 금지.
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
PROBE = "005930"
ORD_QTY = "1"

# 잔고 행 리스트 키 후보(알려진 것 우선) — CLAUDE.md §5 추정
KNOWN_ROW_KEYS = ("acnt_evlt_remn_indv_tot",)
# 행 종목코드 필드명 후보(미검증 — 후보 순회로 강건화)
SYMBOL_KEYS = ("stk_cd", "stk_cd_gb", "stkcd")

# 청산 확인 폴링(반영 지연 대비): 간격들
POLL_BACKOFF = (0.5, 1.0, 1.5, 2.0)


async def _call(client: KiwoomHttpClient, category: str, api_id: str, body: dict):
    """(body, rc, msg) 반환. BrokerError 흡수."""
    try:
        resp, _cont, _next = await client.call(category, api_id, body)
        return resp, str(resp.get("return_code")), resp.get("return_msg")
    except BrokerError as exc:
        return {}, str(getattr(exc, "return_code", "?")), \
            getattr(exc, "return_msg", None) or str(exc)


async def _market_order(client: KiwoomHttpClient, api_id: str) -> tuple[bool, str, dict]:
    """시장가(trde_tp=3) 주문. (접수여부, msg, resp). api_id: kt10000 매수/kt10001 매도."""
    body = {"dmst_stex_tp": "KRX", "stk_cd": PROBE, "ord_qty": ORD_QTY, "trde_tp": "3"}
    resp, rc, msg = await _call(client, "ordr", api_id, body)
    print(f"  {api_id} 시장가(trde_tp=3) → rc={rc} msg={msg!r} ord_no={resp.get('ord_no')!r}")
    return rc == "0", msg or "", resp


def _find_rows(resp: dict) -> tuple[str, list]:
    """잔고 원응답에서 포지션 행 리스트 — 알려진 키 우선, 없으면 첫 리스트(다중 경고)."""
    for k in KNOWN_ROW_KEYS:
        if isinstance(resp.get(k), list):
            return k, resp[k]
    list_keys = [k for k, v in resp.items() if isinstance(v, list)]
    if len(list_keys) > 1:
        print(f"  ⚠️ 리스트형 최상위 키 {len(list_keys)}개 {list_keys} — 첫 키 채택(오판 가능, 로그 확인)")
    return (list_keys[0], resp[list_keys[0]]) if list_keys else ("", [])


async def _held_qty(client: KiwoomHttpClient) -> tuple[bool, bool]:
    """raw kt00018로 PROBE 보유 여부 확인. (보유여부, 확인신뢰) 반환.
    파싱/필드명에 의존하되, rc!=0 또는 행이 dict 아님 등 이상 시 (True, False)로
    **보수 처리**(보유 가능·확인 불신) — 청산 재시도를 유도한다."""
    body = {"qry_tp": "1", "dmst_stex_tp": "KRX"}
    resp, rc, _msg = await _call(client, "acnt", "kt00018", body)
    if rc != "0":
        return True, False  # 조회 실패 → 보수적으로 보유 간주
    try:
        _key, rows = _find_rows(resp)
        if not rows:
            return False, True
        if not all(isinstance(r, dict) for r in rows):
            # 비-dict 행 = 예상과 다른 스키마(엉뚱한 리스트를 골랐을 가능성) → 불신 처리.
            # 조용히 걸러 '0건 청산완료'로 오판하지 않는다(dev 지적).
            print("  ⚠️ 보유확인: 비-dict 행 감지 — 스키마 이상, 보수적으로 보유 간주")
            return True, False
        held = any(
            any(str(r.get(k, "")).endswith(PROBE) for k in SYMBOL_KEYS)
            for r in rows
        )
        return held, True
    except Exception as e:
        print(f"  ⚠️ 보유확인 파싱 예외({type(e).__name__}) — 보수적으로 보유 간주")
        return True, False


async def _wait_cleared(client: KiwoomHttpClient) -> bool:
    """청산됐는지 폴링(백오프). 한 번이라도 '보유 없음+신뢰'면 True."""
    for delay in POLL_BACKOFF:
        await asyncio.sleep(delay)
        held, trusted = await _held_qty(client)
        if trusted and not held:
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
        broker: KiwoomBroker | None = None
        order_accepted = False   # 매수 rc=0 (주문 접수)
        cleared = False          # 청산 확인됨(보유 없음+신뢰)
        try:
            limiter = RateLimiter()
            tm = TokenManager(http, settings.kiwoom_app_key.get_secret_value(),
                              settings.kiwoom_secret_key.get_secret_value(), limiter=limiter)
            client = KiwoomHttpClient(settings, http=http, token_manager=tm, limiter=limiter)
            broker = KiwoomBroker(client)
            await tm.get_token()

            print("=== [사전] 예수금 / 기존 보유 ===")
            dep = await broker.get_deposit()
            print(f"예수금 available={dep.available:,}")
            pre_held, trusted = await _held_qty(client)
            print(f"{PROBE} 기존 보유={pre_held} (신뢰={trusted})")
            if pre_held:
                print("  ‼️ 기존 보유 있음(또는 확인 불가) — 신규 매수 시 수량 혼입. 중단.")
                return

            # [1] 시장가 매수 (체결) — trde_tp=3 검증 겸
            print("\n=== [1] 시장가 매수 kt10000 (1주, 체결) ===")
            order_accepted, msg, _ = await _market_order(client, "kt10000")
            if not order_accepted:
                print(f"  ⚠️ 시장가 매수 실패({msg!r}) — trde_tp='3' 미지원 가능. 중단(청산 불필요).")
                return
            # 체결 반영 폴링(잔고에 나타날 때까지)
            for delay in POLL_BACKOFF:
                await asyncio.sleep(delay)
                held, trusted = await _held_qty(client)
                if held:
                    break

            # [2] 잔고 kt00018 행 단위 필드 실측 (핵심)
            print("\n=== [2] 잔고 kt00018 행 단위 필드 (핵심 실측) ===")
            body = {"qry_tp": "1", "dmst_stex_tp": "KRX"}
            resp, rc, msg = await _call(client, "acnt", "kt00018", body)
            print(f"  rc={rc} msg={msg!r} 최상위 키={sorted(resp.keys())}")
            row_key, rows = _find_rows(resp)
            print(f"  포지션 리스트 키={row_key!r}  raw 행 수={len(rows)}")
            probe_rows = [r for r in rows if isinstance(r, dict)
                          and any(str(r.get(k, "")).endswith(PROBE) for k in SYMBOL_KEYS)]
            if probe_rows:
                r = probe_rows[0]
                print(f"  행 필드({len(r)}개): {sorted(r.keys())}")
                print(f"  {PROBE} 행 원문: {r}")
                for key in ("pur_pric", "avg_price", "avg_prc", "buy_uv"):
                    if key in r:
                        val = str(r[key]).lstrip("+-")
                        print(f"  ▶ {key}={r[key]!r} → {'정수(원 단위)' if '.' not in val else '소수 포함!'}")
            else:
                print(f"  ⚠️ {PROBE} 행 없음 — 체결 미반영/필드명 불일치? 원응답: {resp}")

            # [2b] broker.get_balance() 파싱 대조 — try 격리 + 행 수 비교
            print("\n=== [2b] broker.get_balance() 파싱 대조 (행 수 비교) ===")
            try:
                bal = await broker.get_balance()
                print(f"  파싱된 positions 수={len(bal.positions)} vs raw 행 수={len(rows)}"
                      f"  {'✅ 일치' if len(bal.positions) == len(rows) else '⚠️ 불일치 — 리스트 키/필드명 확인'}")
                for p in bal.positions:
                    if p.symbol.endswith(PROBE):
                        print(f"  Position: symbol={p.symbol} qty={p.quantity} "
                              f"avg_price={p.avg_price} cur={p.current_price} eval={p.eval_amount}")
            except Exception as e:
                print(f"  ⚠️ get_balance 파싱 실패({type(e).__name__}: {e}) — 행 필드명 불일치."
                      f" [2] raw로 broker.py 매핑 수정 필요. 청산은 계속 진행.")

            # [3] 시장가 매도 청산 + 폴링 확인
            print("\n=== [3] 시장가 매도 kt10001 (청산) ===")
            sell_ok, msg, _ = await _market_order(client, "kt10001")
            if sell_ok:
                cleared = await _wait_cleared(client)
                print(f"  청산 확인: {'✅ 완료' if cleared else '⚠️ 미확인 — finally 재시도'}")
            else:
                print(f"  ‼️ 매도 접수 실패({msg!r}) — finally에서 재청산")

            print("\n=== G3 판정 체크리스트 ===")
            print(f"  [{'x' if order_accepted else ' '}] 시장가 매수 체결(trde_tp=3)?")
            print(f"  [{'x' if probe_rows else ' '}] kt00018 행 필드 확보?")
            print(f"  [{'x' if cleared else ' '}] 시장가 매도 청산 확인?")
            print("  [ ] avg_price/pur_pric 원 단위 정수? (위 ▶ 판정)")
            print("  [ ] 리스트 키명 + 행 필드명 (위 로그 판독)")
            print("  [ ] 매도(kt10001) 바디가 매수(kt10000)와 동일 스키마? (rc=0이면 확정)")
        finally:
            # 안전망: 매수 접수됐는데 청산 미확인이면 폴링+재매도 반복(최대 3회)
            if client is not None and order_accepted and not cleared:
                print("\n[안전정리] 청산 미확인 — 보유 확인 후 시장가 매도 반복")
                for attempt in range(3):
                    try:
                        held, trusted = await _held_qty(client)
                        if trusted and not held:
                            print("  ✅ 안전정리: 보유 없음 확인(청산됨)")
                            break
                        print(f"  시도 {attempt+1}: 보유={held} 신뢰={trusted} — 시장가 매도")
                        await _market_order(client, "kt10001")
                        await asyncio.sleep(1.0)
                    except Exception as e:
                        print(f"  ‼️ 안전정리 예외({type(e).__name__}: {e})")
                else:
                    print(f"  ‼️‼️ 청산 미확정 — 수동으로 {PROBE} 보유/미체결 확인·정리 필요")
            if tm is not None:
                await tm.revoke()
                print("[토큰 revoke 완료]")


if __name__ == "__main__":
    asyncio.run(main())
