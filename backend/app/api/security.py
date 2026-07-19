"""쓰기 엔드포인트 보호 — X-API-Key 헤더 검증 (P3/P4 보안 패널 이월,
사용자 결정 2026-07-18: API 키 + CORS 제한).

토큰 미설정 시에는 차단하지 않고 기동 시 경고만 남긴다 — 모의투자
로컬 개발 편의. 실전 전환(KIWOOM_MOCK=false) 시에는 Settings의
model_validator(config.py)가 토큰 미설정을 fail-fast로 차단해 "토큰
설정 필수"를 코드로 강제한다.

CORS 오리진 allowlist(main.py)와 이 X-API-Key 검증은 서로 다른 위협을
막는다 — 오리진 allowlist는 브라우저의 "응답 읽기"만 차단할 뿐, 커스텀
헤더 없는 단순 요청(예: 폼 POST)은 오리진 검증을 통과하지 못해도 이미
서버까지 도달한다(CORS는 CSRF 방어가 아니다). 즉 토큰이 설정되지 않은
상태에서는 CSRF성 쓰기 트리거가 여전히 가능하며, 실질적인 쓰기 실행
차단은 이 X-API-Key 검증이 전담한다(실전 전환 시 위 validator가 필수로
강제)."""

import logging
import secrets

from fastapi import Header, HTTPException, Request

logger = logging.getLogger(__name__)


async def require_write_token(
        request: Request,
        x_api_key: str | None = Header(default=None)) -> None:
    token = request.app.state.settings.api_write_token
    if token is None:
        return  # 미설정 — main.py 기동 시 경고 로그가 이미 남음
    if x_api_key is None:
        logger.warning("write endpoint auth rejected: path=%s reason=%s",
                        request.url.path, "missing")
        raise HTTPException(status_code=401, detail="invalid or missing API key")
    # secrets.compare_digest는 str 인자에 비-ASCII가 섞이면 TypeError를
    # 던진다(CPython 구현 제약) — 401 대신 500으로 새는 것을 막기 위해
    # 바이트 단위로 비교한다(둘 다 encode() 후 비교, 타이밍 공격 내성은
    # compare_digest가 여전히 보장).
    if not secrets.compare_digest(
            token.get_secret_value().encode(), x_api_key.encode()):
        logger.warning("write endpoint auth rejected: path=%s reason=%s",
                        request.url.path, "mismatch")
        raise HTTPException(status_code=401, detail="invalid or missing API key")
