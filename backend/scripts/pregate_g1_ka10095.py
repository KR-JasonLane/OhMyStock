"""Phase 5 PRE-GATE G1 실측 — ka10095 관심종목정보요청 다종목 시세.

실행(모의서버, 장중 09:00~15:30 권장):
  cd backend && uv run python scripts/pregate_g1_ka10095.py > ../.superpowers/sdd/p5-pregate-G1.txt 2>&1

확인 목표(계획서 Task 0 / 스펙 §4 G1):
  1. 세미콜론(;) 구분자로 다종목 조회가 되는가
  2. 1회 최대 종목 수(100 추정) — 상한 초과 시 거동 (+ cont-yn 페이지네이션 여부)
  3. 응답 리스트 키 + 현재가 + **최우선 매수/매도호가(bid/ask) 필드 포함 여부**
  4. 부분 실패(존재하지 않는 코드 혼입) 계약
  5. 실제 소요시간

전제: .env에 모의 발급 키(KIWOOM_MOCK=true). 백엔드/다른 프로세스가 같은 앱키로
가동 중이면 실행하지 말 것(앱키당 활성 토큰 1개 추정 — CLAUDE.md §5, [8005] 사고).
이 스크립트는 자체 TokenManager로 발급하고 종료 시 revoke 한다 — client가 자체
TokenManager를 만들지 않도록 token_manager=tm을 명시 주입한다(그렇지 않으면 두
토큰이 경합해 살아있는 토큰이 미폐기된다 — 보안 패널).

주의: category/필드명은 문서-미검증(웹 리서치 기반)이라 실측으로 확정한다 —
Phase 1·2의 문서-실측 괴리 사례(ka10099 marketCode 혼입 등)와 같은 이유.
client.call()은 return_code!=0을 ApiError로 던지므로(상한초과·부분실패가 바로
그 케이스), 측정 루프는 ApiError를 흡수하고 다음 측정으로 진행한다(개발자 패널).
"""

import asyncio
import time

import httpx

from app.adapters.kiwoom.auth import TokenManager
from app.adapters.kiwoom.broker import KiwoomBroker
from app.adapters.kiwoom.client import KiwoomHttpClient
from app.adapters.kiwoom.rate_limiter import RateLimiter
from app.core.config import Settings
from app.domain.errors import ApiError, BrokerError

MOCK_BASE = "https://mockapi.kiwoom.com"

# ka10095 category는 실측 확정(stkinfo, rc=0 + atn_stk_infr 리스트 키).
CATEGORY = "stkinfo"
API_ID = "ka10095"

# ⚠️ broker-api-expert 진단(1차 실측): 08:38 실행에서 맨코드 단건도 stk_cd/stk_nm까지
# 전부 빈 값 → 서버가 코드 미매칭. 공식 스펙상 ka10095 stk_cd는 거래소 프리픽스
# 형식(KRX:005930, 최대 20자)이라 맨코드가 안 먹힌 것으로 추정. mock은 KRX 전용.
EXCHANGE_PREFIX = "KRX:"

# 프리픽스 프로브용 초유동 종목(호가가 얕지 않아 바인딩 확인에 안정적)
LIQUID = ["005930", "000660", "035420", "005380", "051910"]  # 삼성전자·SK하이닉스·NAVER·현대차·LG화학

# ⚠️ 실측 확정(09:01 장중): ka10095 다종목 구분자는 파이프('|')다 — 세미콜론/
# 콤마/공백은 전부 빈값, 파이프만 다종목 실데이터 반환(웹 문서의 세미콜론은 오류).
DELIM = "|"

# 존재하지 않는 더미 코드 — 부분 실패 계약 확인용(실존 코드와 혼입)
BOGUS_CODE = "000000"


def _px(code: str) -> str:
    """거래소 프리픽스 부착 (이미 붙어있으면 그대로)."""
    return code if ":" in code else EXCHANGE_PREFIX + code


def _is_empty_row(row: dict) -> bool:
    """stk_cd/cur_prc가 공백이면 바인딩 실패(코드 미매칭)로 본다."""
    return not (str(row.get("stk_cd", "")).strip() or str(row.get("cur_prc", "")).strip())

# 계좌·자금 연계로 보이는 필드는 evidence 파일(.txt, gitignore 미등재)에 원문을
# 남기지 않는다 — ka10095는 시세 TR이라 없을 것으로 보이나 스키마 미검증이므로
# 방어(보안 패널). 이번 프로젝트에서 이미 확인된 계좌 필드 어휘 패턴.
_SENSITIVE_HINTS = ("acnt", "bal", "pur_pric", "evlt", "avg", "entr", "ord_alow_amt",
                    "예수금", "잔고", "매입")


def _join(codes: list[str]) -> str:
    return ";".join(codes)


def _mask_sensitive(row: dict) -> dict:
    """계좌/자금성 필드 값을 <마스킹>으로 치환해 출력용 사본을 만든다."""
    out = {}
    for k, v in row.items():
        if any(h in k.lower() for h in _SENSITIVE_HINTS):
            out[k] = "<마스킹>"
        else:
            out[k] = v
    return out


async def _call(client: KiwoomHttpClient, stk_cd_value: str) -> tuple[dict, str, str]:
    """ka10095 1회 호출. (body, cont_yn, next_key) 반환. BrokerError(ApiError/
    RateLimitError/AuthError/네트워크)는 흡수해 body 형태로 표면화한다."""
    try:
        return await client.call(CATEGORY, API_ID, {"stk_cd": stk_cd_value})
    except BrokerError as exc:
        rc = getattr(exc, "return_code", "?")
        msg = getattr(exc, "return_msg", None) or str(exc)
        return {"return_code": rc, "return_msg": msg}, "N", ""


def _find_list_keys(body: dict) -> list[str]:
    """응답에서 비어있지 않은 list 값을 가진 키 전부(오탐 대비 복수 나열)."""
    return [k for k, v in body.items() if isinstance(v, list) and v]


def _report_fields(rows: list[dict]) -> None:
    if not rows:
        print("    (행 없음)")
        return
    sample = rows[0]
    print(f"    행 필드({len(sample)}개): {sorted(sample.keys())}")
    # 호가(bid/ask) 후보 필드 스캔 — 'sell' 전체 단어로 좁혀 오탐 감소
    bid_ask = [k for k in sample
               if any(t in k.lower() for t in ("bid", "ask", "매수", "매도", "호가",
                                               "buy", "sell", "offer"))]
    print(f"    호가 후보 필드: {bid_ask or '없음 (→ 별도 호가 TR 필요 가능)'}")
    print(f"    첫 행 원문(계좌성 필드 마스킹): {_mask_sensitive(sample)}")


async def main() -> None:
    settings = Settings()
    if not settings.kiwoom_mock:
        print("SKIP: KIWOOM_MOCK=true 아님 — 모의서버 전용 실측")
        return

    async with httpx.AsyncClient(base_url=MOCK_BASE, timeout=15) as http:
        tm: TokenManager | None = None
        try:
            # 토큰이 존재할 수 있는 모든 구간을 finally revoke가 커버하도록 try 안에서 구성
            limiter = RateLimiter()  # tm·client가 같은 버킷 공유(토큰 발급도 레이트리밋)
            tm = TokenManager(http, settings.kiwoom_app_key.get_secret_value(),
                              settings.kiwoom_secret_key.get_secret_value(), limiter=limiter)
            client = KiwoomHttpClient(settings, http=http, token_manager=tm, limiter=limiter)
            broker = KiwoomBroker(client)

            token = await tm.get_token()
            assert token  # 값은 출력하지 않는다

            # [0] 다종목 구분자 매트릭스 — 08:56 실측: 맨코드 단건=실데이터,
            #     프리픽스·세미콜론=빈값. 프리픽스 가설 반증됨(맨코드가 정답).
            #     핵심 미결: 다종목 조회가 되는 구분자가 있는가(결정 #27 좌우).
            #     초유동 2종목으로 세미콜론/콤마/공백/파이프를 비교.
            print("=== [0] 다종목 구분자 매트릭스 (맨코드 기준, 초유동 2종목) ===")
            a, b = "005930", "000660"  # 삼성전자, SK하이닉스
            matrix = {
                "단건            005930": a,
                "세미콜론    005930;000660": f"{a};{b}",
                "콤마        005930,000660": f"{a},{b}",
                "공백        005930 000660": f"{a} {b}",
                "파이프      005930|000660": f"{a}|{b}",
            }
            single_ok = False
            for label, value in matrix.items():
                body, cont, nxt = await _call(client, value)
                lks = _find_list_keys(body)
                rows = body[lks[0]] if lks else []
                empties = sum(_is_empty_row(r) for r in rows)
                verdict = "빈값(미바인딩)" if rows and empties == len(rows) else \
                          ("일부빈값" if empties else "실데이터 OK")
                nonempty = len(rows) - empties
                print(f"  {label:<24} → {len(rows)}행 (실데이터 {nonempty}) [{verdict}] "
                      f"rc={body.get('return_code')!r} cont-yn={cont!r}")
                if label.startswith("단건") and rows and empties == 0:
                    single_ok = True

            # [1] 응답 구조/필드 — 맨코드 단건(실데이터 확인된 형태)
            print("\n=== [1] 응답 구조·호가 필드 (맨코드 단건 005930) ===")
            body, cont, nxt = await _call(client, a)
            print(f"rc={body.get('return_code')!r} msg={body.get('return_msg')!r} "
                  f"cont-yn={cont!r} next-key={nxt!r}")
            list_keys = _find_list_keys(body)
            print(f"리스트 키 후보: {list_keys}")
            if list_keys and body[list_keys[0]]:
                _report_fields(body[list_keys[0]])

            if not single_ok:
                print("\n⚠️ 맨코드 단건조차 실데이터 미반환 — 이후 측정 생략.")
                print("   → stk_nm까지 빈값이면 형식/바인딩 문제, stk_nm만 차면 장중 실시간 이슈.")
            else:
                # [2] 종목 수 스케일 (맨코드, 파이프 구분자) — 상한 + 페이지네이션
                print("\n=== [2] 종목 수 스케일 (맨코드 파이프, 반환 행/cont-yn/시간) ===")
                instruments = await broker.list_instruments("kospi")
                codes = [i.symbol for i in instruments][:200]
                for n in (1, 50, 100, 101, 150, 200):
                    if n > len(codes):
                        continue
                    t0 = time.monotonic()
                    body, cont, nxt = await _call(client, DELIM.join(codes[:n]))
                    dt = time.monotonic() - t0
                    lks = _find_list_keys(body)
                    rows = body[lks[0]] if lks else []
                    ne = sum(not _is_empty_row(r) for r in rows)
                    paged = " [cont-yn=Y 페이지네이션!]" if cont == "Y" else ""
                    print(f"  요청 {n:>3}종목 → 반환 {len(rows):>3}행(실데이터 {ne})  "
                          f"rc={body.get('return_code')!r} {dt*1000:.0f}ms  cont-yn={cont!r}{paged}")

                # [3] 부분 실패 — 더미 코드 혼입(파이프)
                print("\n=== [3] 부분 실패 계약 (더미 코드 혼입) ===")
                body, cont, nxt = await _call(client, f"{a}{DELIM}{BOGUS_CODE}{DELIM}{b}")
                lks = _find_list_keys(body)
                rows = body[lks[0]] if lks else []
                print(f"요청 3종목(1 더미) → 반환 {len(rows)}행  rc={body.get('return_code')!r}")
                if rows:
                    returned = {r.get("stk_cd") for r in rows}
                    print(f"반환 코드 집합: {returned}  (더미 {BOGUS_CODE} 포함 여부)")

            print("\n=== G1 판정 체크리스트 ===")
            print("  [ ] 프리픽스(KRX:) 단건 바인딩 성공? (→ [0] 매트릭스)")
            print("  [ ] 세미콜론 다종목 조회 성공? (프리픽스 다건 행 수 >1?)")
            print("  [ ] 최대 종목 수 상한? (반환 행 수 + cont-yn)")
            print("  [x] 호가(bid/ask) 필드 포함? → 실측 확정: sel_1~5th_bid/buy_1~5th_bid 포함")
            print("  [ ] 부분 실패 시 성공분만 반환?")
            print("  [ ] → 세미콜론 다종목 불발이면 스펙 결정 #27(감시 아키텍처) 재결정")
        finally:
            if tm is not None:
                await tm.revoke()  # 발급 토큰 폐기 (앱키당 1토큰 — 다음 실측 오염 방지)
                print("\n[토큰 revoke 완료]")


if __name__ == "__main__":
    asyncio.run(main())
