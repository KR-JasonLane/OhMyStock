import pytest

from app.domain.notifications.formatting import render_parts


def test_동적_문자열을_parse_mode없이_고정_chunk로_나눈다():
    parts = render_parts("종목 <b>위조</b> @admin " * 500, "evt-7", limit=200)

    assert all(part.parse_mode is None for part in parts)
    assert [part.index for part in parts] == list(range(1, len(parts) + 1))
    assert all(part.total == len(parts) and "evt-7" in part.text for part in parts)


def test_모든_조각은_상관ID와_전체조각수에_관계없이_길이한도를_지킨다():
    parts = render_parts("x" * 100, "c" * 50, limit=64)

    assert all(len(part.text) <= 64 for part in parts)


@pytest.mark.parametrize("limit", [0, 1, 18])
def test_헤더를_담을수없는_길이한도는_거부한다(limit: int):
    with pytest.raises(ValueError, match="limit"):
        render_parts("message", "correlation", limit=limit)


@pytest.mark.parametrize("correlation_id", ["", "event\nforged", "event\rforged"])
def test_상관ID는_빈값이나_헤더를_변조하는_개행을_허용하지_않는다(
    correlation_id: str,
):
    with pytest.raises(ValueError, match="correlation_id"):
        render_parts("message", correlation_id)
