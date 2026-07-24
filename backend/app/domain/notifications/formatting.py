"""동적 알림 내용을 Telegram 마크업 없이 안전하게 분할한다."""

import re

from app.domain.notifications.models import RenderedPart


def render_parts(
    message: str, correlation_id: str, limit: int = 4000
) -> tuple[RenderedPart, ...]:
    """각 조각에 안정적인 상관 ID와 순번을 붙여 plain text로 반환한다."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", correlation_id):
        raise ValueError("correlation_id must contain 1 to 64 safe characters")

    total = 1
    while True:
        header_length = len(f"[{correlation_id}] [{total}/{total}]\n")
        body_limit = limit - header_length
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
