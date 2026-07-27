import pytest

from app.domain.notifications.models import CommandKind, InvalidCommand
from app.domain.notifications.parsing import parse_command


def test_analysis는_인자없는_조회명령이다():
    assert parse_command("/analysis").kind is CommandKind.ANALYSIS
    with pytest.raises(InvalidCommand):
        parse_command("/analysis now")


def test_digest는_인자없는_조회명령이다():
    assert parse_command("/digest").kind is CommandKind.DIGEST
    with pytest.raises(InvalidCommand):
        parse_command("/digest now")
