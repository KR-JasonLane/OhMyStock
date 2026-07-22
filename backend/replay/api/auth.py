"""oauth2 토큰 엔드포인트(스펙 §7).

시크릿 무로그 계약(보안 패널 #2): 요청 바디의 appkey/secretkey는 **검증
없이 즉시 폐기**한다 — 변수에 바인딩하지 않고, 로그·응답·예외 메시지에
절대 싣지 않는다(가짜 토큰 발급이 목적이지 자격 검증이 아니다). 회귀
테스트가 caplog/응답에 시크릿 문자열 부재를 단정한다."""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from replay.api.common import OK_MSG

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/oauth2/token")
async def issue_token(request: Request) -> JSONResponse:
    # 바디는 읽되(콘텐츠 소비) 필드는 사용하지 않는다 — appkey/secretkey
    # 즉시 폐기(무로그 계약). grant_type 검증도 생략: 목의 관심사는 토큰
    # 수명 의미론(단일 활성·8005)이지 자격 증명이 아니다.
    await request.body()
    token, expires_dt = request.app.state.tokens.issue()
    # 실측 형태(Phase 1): token + return_code + expires_dt(절대 KST)
    return JSONResponse({
        "token": token,
        "token_type": "Bearer",
        "expires_dt": expires_dt,
        "return_code": 0,
        "return_msg": OK_MSG,
    })


@router.post("/oauth2/revoke")
async def revoke_token(request: Request) -> JSONResponse:
    # 실측: {appkey, secretkey, token} 수신 → return_code 0. 시크릿은
    # 여기서도 미사용 — token 필드만 소비. 파싱 실패는 500 예외 경로를
    # 만들지 않고 관용(보안 R4 Minor — 예외 경로 자체를 제거하는 방어
    # 심층화; 시크릿이 스택트레이스에 실릴 여지 원천 차단).
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    token = str(payload.get("token", "")) if isinstance(payload, dict) else ""
    if not request.app.state.tokens.revoke(token):
        # 미지/이미 무효 토큰 — 성공 응답은 유지하되 관측은 남긴다
        # (tokens.revoke docstring 계약 — 개발자 R4 Minor 정합)
        logger.debug("revoke for unknown/stale token — tolerated (rc=0)")
    return JSONResponse({"return_code": 0, "return_msg": OK_MSG})
