"""동적 알림 내용을 Telegram 마크업 없이 안전하게 분할한다."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.domain.notifications.models import RenderedPart


_KST = ZoneInfo("Asia/Seoul")
_DELAY_NOTICE_PREFIX = "[지연 알림] 발생 시각: "
# ISO-8601 KST seconds precision header plus newline.  The formatter's default
# limit reserves this maximum before the sender knows whether delivery delays.
_DELAY_NOTICE_RESERVE = 64


def render_parts(
    message: str, correlation_id: str, limit: int = 4000
) -> tuple[RenderedPart, ...]:
    """각 조각에 안정적인 상관 ID와 순번을 붙여 plain text로 반환한다."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", correlation_id):
        raise ValueError("correlation_id must contain 1 to 64 safe characters")

    total = 1
    while True:
        header_length = len(f"[{correlation_id}] [{total}/{total}]\n")
        # Telegram 기본 한도에서는 sender가 지연 표지를 붙일 여유를 미리
        # 확보한다. 작은 임의 limit은 순수 formatter 경계 테스트용이므로
        # 기존의 최소 header 계약을 유지한다.
        delay_reserve = _DELAY_NOTICE_RESERVE if limit >= 4000 else 0
        body_limit = limit - header_length - delay_reserve
        if body_limit <= 0:
            raise ValueError("limit is too small for the rendered part header")
        chunks = tuple(
            message[index : index + body_limit]
            for index in range(0, len(message), body_limit)
        ) or ("",)
        if len(chunks) == total:
            break
        total = len(chunks)

    return tuple(
        RenderedPart(index, total, f"[{correlation_id}] [{index}/{total}]\n{chunk}")
        for index, chunk in enumerate(chunks, 1)
    )


def delay_notice(text: str, occurred_at: datetime, *, limit: int = 4096) -> str:
    """Long-delayed critical delivery의 plain-text 표지를 안전하게 붙인다."""
    notice = delay_notice_header(occurred_at)
    if text.startswith(_DELAY_NOTICE_PREFIX):
        return text
    rendered = notice + text
    if len(rendered) > limit:
        raise ValueError("rendered delayed message exceeds limit")
    return rendered


def delay_notice_header(occurred_at: datetime) -> str:
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise ValueError("occurred_at must be timezone-aware")
    rendered_at = occurred_at.astimezone(_KST).isoformat(timespec="seconds")
    return f"{_DELAY_NOTICE_PREFIX}{rendered_at}\n"
