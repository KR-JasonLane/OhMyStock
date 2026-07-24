"""운영자와 개인 채팅을 모두 확인하는 순수 인증 판정."""

from collections.abc import Collection

from app.domain.notifications.models import InboundMessage, OperatorIdentity


def is_authorized(
    message: InboundMessage, operators: Collection[OperatorIdentity]
) -> bool:
    return (
        message.chat_type == "private"
        and not message.forwarded
        and any(
            operator.user_id == message.user_id and operator.chat_id == message.chat_id
            for operator in operators
        )
    )
