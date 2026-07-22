"""리플레이 목 서버용 ka10080(분봉) 실측 프로브 + 수집 스크립트.

실행(모의서버, 장 마감 후 권장 — 확정 분봉):
  cd backend && uv run python scripts/replay_collect_minutes.py \
      > ../.superpowers/sdd/replay-ka10080-probe.txt 2>&1

1부(프로브 — 첫 실측, 리플레이 스펙 §6/§11-1):
  1. 요청 바디 필드명: stk_cd / tic_scope(분 단위) / upd_stkpc_tp(수정주가)
     — 문서-미검증(공식문서+랩퍼 리서치 기반)이라 실측으로 확정한다
     (ka10081 base_dt 필수 같은 문서-실측 괴리 전례 대비)
  2. 응답 리스트 키(추정 stk_min_pole_chart_qry)와 행 필드명/시각 포맷
  3. 정렬 방향(일봉은 내림차순 실측 — 분봉도 확인)
  4. cont-yn/next-key 페이지네이션 왕복 + **보관 일수**(가장 오래된 분봉)
  5. 빈/결측 분(거래 없는 분)의 표현 방식

2부(수집): 프로브가 성공하면 대상 종목 전체를 페이지네이션으로 끝까지 수집해
backend/replay/data/minutes.sqlite 에 저장(gitignore — 운영 DB와 분리, 스펙 §6).
수집 원본 형태(정렬·문자열)는 raw 그대로 두고 파싱은 로더(replay/minute_store.py)가
담당 — 프로브 단계에서 형태 가정을 최소화한다.

전제: .env 모의 키. 같은 앱키로 백엔드/타 프로세스 가동 중이면 실행 금지
(활성 토큰 1개 — [8005] 사고, CLAUDE.md §5). 자체 TokenManager 주입 + 종료 시
revoke(G1 하네스와 동일 — 미주입 시 이중 토큰 경합으로 미폐기 토큰이 남는다).
"""

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import httpx

from app.adapters.kiwoom.auth import TokenManager
from app.adapters.kiwoom.client import MOCK_BASE, KiwoomHttpClient
from app.adapters.kiwoom.rate_limiter import RateLimiter
from app.core.config import Settings
from app.domain.errors import ApiError, BrokerError

CATEGORY = "chart"      # 일봉 ka10081과 동일 카테고리(실측) — 분봉도 chart 추정
API_ID = "ka10080"

# 리플레이 대상(스펙 §6): 커버리지 — 코스피 대형4, 코스닥3(변동성),
# ETF2(거래세 면제 경로), 저유동성1, 우선주1. 최종 확정은 §6 임계 크로스
# 커버리지 게이트가 판정(미충족 시 재선정).
SYMBOLS = [
    "005930",  # 삼성전자 (코스피 대형)
    "000660",  # SK하이닉스 (코스피 대형)
    "005380",  # 현대차 (코스피)
    "051910",  # LG화학 (코스피)
    "247540",  # 에코프로비엠 (코스닥 변동성)
    "196170",  # 알테오젠 (코스닥)
    "035760",  # CJ ENM (코스닥)
    "069500",  # KODEX 200 (ETF — 거래세 면제)
    "371460",  # TIGER 차이나전기차 (ETF)
    "005935",  # 삼성전자우 (우선주)
    "012510",  # 더존비즈온 — P2 degenerate 캔들 전례 종목(결함 응답 커버리지)
    "028300",  # HLB (코스닥)
]

# 후보 요청 바디(1부에서 실측 확정). tic_scope "1"=1분.
# ⚠️ broker-api 리서치: ka10080에 "기준일자" 파라미터가 최근 추가된 정황
# (사제 랩퍼 v0.7.0 changelog — ka10081 base_dt 필수 선례와 동일 패턴).
# base_dt 포함 후보를 최우선으로 두고, 1511(필수값 누락) 거부 시 다음 후보.
# (누락 오류는 필드를 더해야 고쳐진다 — 후보 순서는 many→few 필드 순.)
BODY_CANDIDATES = [
    {"stk_cd": "{code}", "tic_scope": "1", "upd_stkpc_tp": "1",
     "base_dt": "{today}"},
    {"stk_cd": "{code}", "tic_scope": "1", "upd_stkpc_tp": "1"},
    {"stk_cd": "{code}", "tic_scope": "1", "base_dt": "{today}"},
]

DB_PATH = Path(__file__).resolve().parents[1] / "replay" / "data" / "minutes.sqlite"
MAX_PAGES = 200  # 안전 상한(무한 페이지 가드) — 도달 시 경고 출력


def _today_kst() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")


def _fmt_body(template: dict, code: str) -> dict:
    today = _today_kst()
    return {k: v.format(code=code, today=today) if isinstance(v, str) else v
            for k, v in template.items()}


def _list_key(resp: dict) -> str | None:
    for key, value in resp.items():
        if isinstance(value, list) and value:
            return key
    return None


async def probe(client: KiwoomHttpClient) -> tuple[dict, str] | None:
    """바디 후보를 순서대로 시도 — 성공한 (바디 템플릿, 리스트 키) 반환.
    client.call은 (dict, cont_yn, next_key) 3-튜플(어댑터 실시그니처)."""
    for template in BODY_CANDIDATES:
        body = _fmt_body(template, SYMBOLS[0])
        print(f"\n[probe] body={body}")
        try:
            resp, cont, next_key = await client.call(CATEGORY, API_ID, body)
        except ApiError as exc:
            # rc!=0(필수값 누락 등) — 바디 후보 문제일 가능성, 다음 후보 시도
            print(f"  -> rejected (api rc!=0): {exc}")
            continue
        except BrokerError as exc:
            # 전송/인증 계열 — 바디 문제가 아니므로 후보 순회 무의미(오진단 방지)
            print(f"  -> transport/auth error (후보 순회 중단): {exc}")
            return None
        key = _list_key(resp)
        print(f"  -> top-level keys: {list(resp.keys())}")
        print(f"  -> list key: {key}")
        if key:
            rows = resp[key]
            print(f"  -> rows: {len(rows)}")
            print(f"  -> first row: {json.dumps(rows[0], ensure_ascii=False)}")
            print(f"  -> last row:  {json.dumps(rows[-1], ensure_ascii=False)}")
            print(f"  -> cont-yn={cont!r} next-key={next_key!r}")
            return template, key
    return None


async def collect_symbol(client: KiwoomHttpClient, template: dict, key: str,
                         code: str, conn: sqlite3.Connection) -> tuple[int, str]:
    """한 종목 전체 페이지 수집 — (행 수, 가장 오래된 시각 원문) 반환."""
    body = _fmt_body(template, code)
    total, oldest = 0, ""
    cont, next_key = "N", ""
    for page in range(MAX_PAGES):
        try:
            resp, cont, next_key = await client.call(
                CATEGORY, API_ID, body, cont_yn=cont, next_key=next_key)
        except BrokerError as exc:  # ApiError 포함(서브클래스)
            print(f"  [{code}] page {page} error: {exc}")
            break
        rows = resp.get(key) or []
        for row in rows:
            conn.execute(
                "INSERT OR REPLACE INTO minute_raw(symbol, page, seq, row) "
                "VALUES (?,?,?,?)",
                (code, page, total, json.dumps(row, ensure_ascii=False)))
            total += 1
            oldest = str(row)[:200]  # 마지막(가장 오래된 추정) 행 스냅샷
        if cont != "Y" or not next_key:
            break
    else:
        print(f"  [{code}] ⚠️ MAX_PAGES({MAX_PAGES}) 도달 — 수집 미완 가능")
    conn.commit()
    return total, oldest


async def main() -> None:
    settings = Settings()
    if not settings.kiwoom_mock:
        print("SKIP: KIWOOM_MOCK=true 아님 — 모의서버 전용")  # G2 관례(assert 금지)
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS minute_raw("
                 "symbol TEXT, page INTEGER, seq INTEGER, row TEXT, "
                 "PRIMARY KEY(symbol, seq))")

    async with httpx.AsyncClient(base_url=MOCK_BASE, timeout=15.0) as http:
        tm: TokenManager | None = None
        try:
            # 생성 자체를 try 안에서(G1 관례 — 부분 실패에도 finally revoke 보장)
            # limiter는 토큰·TR 호출 공유(버킷 분리 시 실측 왜곡)
            limiter = RateLimiter()
            tm = TokenManager(http,
                              settings.kiwoom_app_key.get_secret_value(),
                              settings.kiwoom_secret_key.get_secret_value(),
                              limiter=limiter)
            client = KiwoomHttpClient(settings, http=http, token_manager=tm,
                                      limiter=limiter)
            print("=== 1부: ka10080 프로브 ===")
            found = await probe(client)
            if found is None:
                print("!! 전 바디 후보 거부 — 필드명 재조사 필요")
                return
            template, key = found
            print(f"\n=== 2부: 수집 (list key={key}) ===")
            started = time.monotonic()
            for code in SYMBOLS:
                count, oldest = await collect_symbol(client, template, key,
                                                     code, conn)
                print(f"  [{code}] rows={count} oldest~={oldest}")
            print(f"총 소요: {time.monotonic() - started:.1f}s → {DB_PATH}")
        finally:
            if tm is not None:
                await tm.revoke()
                print("[토큰 revoke 완료]")
            conn.close()


if __name__ == "__main__":
    asyncio.run(main())
