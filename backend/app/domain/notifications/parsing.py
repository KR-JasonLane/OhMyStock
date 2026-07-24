"""Telegram DTO를 모르는 명령 텍스트 파서."""

import re

from app.domain.notifications.models import CommandKind, InvalidCommand, ParsedCommand


def parse_command(text: str) -> ParsedCommand:
    """허용된 slash 명령만 정규화하고 확인 토큰 문법을 검증한다."""
    if len(text) > 256:
        raise InvalidCommand("command text too long")

    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        raise InvalidCommand("missing command")

    command = parts[0].split("@", 1)[0].removeprefix("/")
    try:
        kind = CommandKind(command)
    except ValueError as exc:
        raise InvalidCommand("unsupported command") from exc

    if kind is CommandKind.CONFIRM:
        if len(parts) != 2 or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", parts[1]):
            raise InvalidCommand("invalid confirmation token")
        return ParsedCommand(kind, parts[1])

    if len(parts) != 1:
        raise InvalidCommand("unexpected command arguments")
    return ParsedCommand(kind)
