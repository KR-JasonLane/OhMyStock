"""api 계층 공통 — 키움 응답 봉투/실측 표기 재현 헬퍼(스펙 §7).

표기 재현 원칙: 값의 **형태**(±부호 프리픽스, 제로패딩 폭)는 실측 캡처와
동일해야 한다 — 프로덕션 파서가 abs/strip을 수행하는지가 검증 대상이므로,
목이 "깨끗한" 값을 주면 파서 결함이 통과해버린다."""

import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse

from replay.tokens import INVALID_TOKEN_MSG, INVALID_TOKEN_RC

OK_MSG = "정상적으로 처리되었습니다"


def kiwoom_json(payload: dict, cont_yn: str = "N",
                next_key: str = "") -> JSONResponse:
    """TR 공통 봉투: HTTP 200 + return_code/return_msg 바디,
    cont-yn/next-key 응답 헤더(실측 TR 패턴 — CLAUDE.md §5)."""
    body = {"return_code": 0, "return_msg": OK_MSG}
    body.update(payload)
    return JSONResponse(body, headers={"cont-yn": cont_yn,
                                       "next-key": next_key})


def kiwoom_error(return_code: int, return_msg: str) -> JSONResponse:
    """실측 오류 계약: 오류도 HTTP 200 + return_code≠0(401/4xx 아님)."""
    return JSONResponse({"return_code": return_code, "return_msg": return_msg},
                        headers={"cont-yn": "N", "next-key": ""})


def bearer_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


async def apply_api_fault(request: Request, api_id: str) -> JSONResponse | None:
    """§9 API 계층 결함(간헐 500/429/지연) — TR 핸들러 진입 최우선 적용.
    토큰 검증보다 **먼저**: 레이트리밋/게이트웨이 장애는 인증 이전 계층의
    현상이므로 무효 토큰 요청에도 동일하게 발생해야 실서버와 같다."""
    fault = request.app.state.faults.api_response_fault(api_id)
    if fault is None:
        return None
    if fault["delay_sec"] > 0:
        await asyncio.sleep(fault["delay_sec"])   # 타임아웃 재현(벽시계)
    if fault["mode"] == "http500":
        return JSONResponse({"error": "replay injected fault"},
                            status_code=500)
    if fault["mode"] == "http429":
        return JSONResponse({"error": "replay injected rate limit"},
                            status_code=429)
    return None   # "delay" — 지연만 주고 정상 진행


def require_token(request: Request) -> JSONResponse | None:
    """TR 공통 토큰 검증(횡단 관심사 단일화 — 개발자 R4 Minor). 무효면
    실측 8005 응답(HTTP 200 + rc=3), 유효면 None."""
    if not request.app.state.tokens.is_valid(bearer_token(request)):
        return kiwoom_error(INVALID_TOKEN_RC, INVALID_TOKEN_MSG)
    return None


def signed(value: int) -> str:
    """ka10095/분봉류 가격 표기: ±부호 프리픽스(실측 — 등락 방향 관례.
    파서는 abs 처리해야 하며, 목은 항상 부호를 붙여 그 계약을 강제한다)."""
    return f"+{value}" if value >= 0 else str(value)


def pad_int(value: int, width: int) -> str:
    """kt00018류 제로패딩 정수 문자열('000000000272750'). 음수는 실측
    ('-00000000002694')처럼 부호가 폭에 포함된다."""
    if value < 0:
        return "-" + str(-value).zfill(width - 1)
    return str(value).zfill(width)


def pad_dec(value: float, width: int, decimals: int = 2) -> str:
    """kt00018 비율 표기('000000100.00', '-00000000.99') — 소수 자리 고정
    제로패딩, 부호 폭 포함."""
    text = f"{abs(value):.{decimals}f}"
    if value < 0:
        return "-" + text.zfill(width - 1)
    return text.zfill(width)


def prev_day_close(store, symbol: str, now) -> int | None:
    """전일 종가 — 재생 당일 자정 이전 마지막 분봉의 close(§5 미래 누출
    안전: last_at_or_before만 사용). flu_rt/pred_pre/base_pric(ka10095)과
    pred_close_pric(kt00018)의 실값 원천(broker-api R4 — 하드코딩 0%는
    등락률 소비 로직의 버그를 통과시키는 구조적 구멍). 선적재 구간에
    전일이 없으면 None(호출측 보합 폴백)."""
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    candle = store.last_at_or_before(symbol, day_start)
    return candle.close if candle else None
