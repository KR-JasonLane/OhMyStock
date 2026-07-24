import hashlib
import json
from datetime import UTC, datetime

import pytest

from app.domain.notifications.authorization import is_authorized
from app.domain.notifications.models import (
    CommandKind,
    InboundMessage,
    InvalidCommand,
    OperationalEvent,
    OperatorIdentity,
    RenderedPart,
)
from app.domain.notifications.parsing import parse_command


def test_confirm은_원문을_보존하지_않고_해시만_반환한다():
    raw_token = "AbCdEf0123456789"
    parsed = parse_command(f"/confirm {raw_token}")

    assert parsed.kind is CommandKind.CONFIRM
    assert parsed.argument_hash == hashlib.sha256(raw_token.encode()).hexdigest()
    assert not hasattr(parsed, "argument")
    assert raw_token not in repr(parsed)
    assert raw_token not in vars(parsed).values()


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "/",
        "/unknown",
        "/status extra",
        "/stop@OtherBot",
        "/pause@",
        "/" + "s" * 256,
    ],
)
def test_빈값과_미지원명령은_InvalidCommand다(text: str):
    with pytest.raises(InvalidCommand):
        parse_command(text)


def test_private_user와_chat이_모두_일치해야_한다():
    allowed = (OperatorIdentity(user_id=10, chat_id=20),)

    assert is_authorized(InboundMessage(1, 10, 20, "private", "/status", False), allowed)
    assert not is_authorized(InboundMessage(2, 10, 21, "private", "/status", False), allowed)
    assert not is_authorized(InboundMessage(3, 10, 20, "group", "/status", False), allowed)
    assert not is_authorized(
        InboundMessage(4, 10, 20, "private", "/status", forwarded=True), allowed
    )


def test_inbound_message는_forwarded_여부를_반드시_명시해야_한다():
    with pytest.raises(TypeError):
        InboundMessage(1, 10, 20, "private", "/status")


@pytest.mark.parametrize("forwarded", [None, 0, ""])
def test_inbound_message는_bool이아닌_forwarded를_거부한다(forwarded: object):
    with pytest.raises(ValueError, match="forwarded"):
        InboundMessage(1, 10, 20, "private", "/status", forwarded)


def test_operational_event는_중첩_payload를_불변_JSON_스냅샷으로_보관한다():
    source_payload = {"nested": {"items": ["before"]}}
    event = OperationalEvent(
        kind="entry_filled",
        source_type="trade_order",
        source_id=7,
        version="1",
        payload=source_payload,
        occurred_at=datetime.now(UTC),
    )

    source_payload["nested"]["items"].append("after")

    assert event.payload["nested"]["items"] == ("before",)
    with pytest.raises(TypeError):
        event.payload["new"] = "value"
    with pytest.raises(TypeError):
        event.payload["nested"]["new"] = "value"


def test_operational_event는_저장용_mutable_JSON_복사본을_반환한다():
    event = OperationalEvent(
        kind="entry_filled",
        source_type="trade_order",
        source_id=7,
        version="1",
        payload={"nested": {"items": ["before"]}},
        occurred_at=datetime.now(UTC),
    )

    storage_payload = event.payload_for_storage()
    json.dumps(storage_payload)
    storage_payload["nested"]["items"].append("after")

    assert event.payload["nested"]["items"] == ("before",)


def test_operational_event는_동결_payload로_다시생성할수있다():
    original = OperationalEvent(
        kind="entry_filled",
        source_type="trade_order",
        source_id=7,
        version="1",
        payload={"nested": {"items": ["before"]}},
        occurred_at=datetime.now(UTC),
    )

    copied = OperationalEvent(
        kind=original.kind,
        source_type=original.source_type,
        source_id=original.source_id,
        version=original.version,
        payload=original.payload,
        occurred_at=original.occurred_at,
    )

    assert copied.payload_for_storage() == {"nested": {"items": ["before"]}}


@pytest.mark.parametrize("payload", [{1: "invalid-key"}, {"value": float("nan")}])
def test_operational_event는_JSON이아닌_키와_scalar를_거부한다(
    payload: dict[object, object],
):
    with pytest.raises(ValueError, match="payload"):
        OperationalEvent(
            "entry_filled", "trade_order", 7, "1", payload, datetime.now(UTC)
        )


def test_operational_event는_빈식별자와_naive_datetime을_거부한다():
    with pytest.raises(ValueError):
        OperationalEvent("", "trade_order", 7, "1", {}, datetime.now(UTC))
    with pytest.raises(ValueError):
        OperationalEvent(
            "entry_filled", "trade_order", 7, "1", {}, datetime.now()
        )


@pytest.mark.parametrize("index,total", [(0, 1), (2, 1), (1, 0)])
def test_rendered_part는_유효한_순번범위만_허용한다(index: int, total: int):
    with pytest.raises(ValueError):
        RenderedPart(index=index, total=total, text="message")
