"""쓰기 엔드포인트 보호 — X-API-Key 헤더 검증 (P3/P4 보안 패널 이월,
사용자 결정 2026-07-18: API 키 + CORS 제한).

토큰 미설정 시에는 차단하지 않고 기동 시 경고만 남긴다 — 모의투자
로컬 개발 편의. Phase 5 실전 전환 게이트에서 "토큰 설정 필수"로
승격한다(STATUS.md PRE-GATE #7과 함께 재평가)."""

import secrets

from fastapi import Header, HTTPException, Request


async def require_write_token(
        request: Request,
        x_api_key: str | None = Header(default=None)) -> None:
    token = request.app.state.settings.api_write_token
    if token is None:
        return  # 미설정 — main.py 기동 시 경고 로그가 이미 남음
    if x_api_key is None or not secrets.compare_digest(
            token.get_secret_value(), x_api_key):
        raise HTTPException(status_code=401, detail="invalid or missing API key")
