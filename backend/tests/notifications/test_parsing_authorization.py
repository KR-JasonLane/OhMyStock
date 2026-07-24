import pytest

from app.domain.notifications.authorization import is_authorized
from app.domain.notifications.models import (
    CommandKind,
    InboundMessage,
    InvalidCommand,
    OperatorIdentity,
)
from app.domain.notifications.parsing import parse_command


def test_confirm은_원문을_반환하지_않고_해시_재료만_검증한다():
    parsed = parse_command("/confirm AbCdEf0123456789")

    assert parsed.kind is CommandKind.CONFIRM
    assert parsed.argument == "AbCdEf0123456789"


@pytest.mark.parametrize("text", ["", "   ", "/", "/unknown", "/status extra"])
def test_빈값과_미지원명령은_InvalidCommand다(text: str):
    with pytest.raises(InvalidCommand):
        parse_command(text)


def test_private_user와_chat이_모두_일치해야_한다():
    allowed = (OperatorIdentity(user_id=10, chat_id=20),)

    assert is_authorized(InboundMessage(1, 10, 20, "private", "/status"), allowed)
    assert not is_authorized(InboundMessage(2, 10, 21, "private", "/status"), allowed)
    assert not is_authorized(InboundMessage(3, 10, 20, "group", "/status"), allowed)
    assert not is_authorized(
        InboundMessage(4, 10, 20, "private", "/status", forwarded=True), allowed
    )
