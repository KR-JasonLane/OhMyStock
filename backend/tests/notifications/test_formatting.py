from datetime import datetime, timezone

import pytest

from app.domain.notifications.formatting import delay_notice, render_parts


def test_동적_문자열을_parse_mode없이_고정_chunk로_나눈다():
    parts = render_parts("종목 <b>위조</b> @admin " * 500, "evt-7", limit=200)

    assert all(part.parse_mode is None for part in parts)
    assert [part.index for part in parts] == list(range(1, len(parts) + 1))
    assert all(part.total == len(parts) and "evt-7" in part.text for part in parts)


def test_모든_조각은_상관ID와_전체조각수에_관계없이_길이한도를_지킨다():
    parts = render_parts("x" * 100, "c" * 50, limit=64)

    assert all(len(part.text) <= 64 for part in parts)


def test_지연표시는_분할때_예약된공간을_써도_길이한도를_넘지않는다():
    parts = render_parts("x" * 5000, "delay", limit=4000)

    occurred_at = datetime(2026, 7, 24, tzinfo=timezone.utc)
    assert all(len(delay_notice(part.text, occurred_at, limit=4000)) <= 4000
               for part in parts)
    assert delay_notice("body", occurred_at).startswith(
        "[지연 알림] 발생 시각: 2026-07-24T09:00:00+09:00")


@pytest.mark.parametrize("limit", [0, 1, 18])
def test_헤더를_담을수없는_길이한도는_거부한다(limit: int):
    with pytest.raises(ValueError, match="limit"):
        render_parts("message", "correlation", limit=limit)


@pytest.mark.parametrize(
    "correlation_id",
    ["", "event\nforged", "event\rforged", "event:7", "x" * 65],
)
def test_상관ID는_안전문자와_64자_범위만_허용한다(
    correlation_id: str,
):
    with pytest.raises(ValueError, match="correlation_id"):
        render_parts("message", correlation_id)
