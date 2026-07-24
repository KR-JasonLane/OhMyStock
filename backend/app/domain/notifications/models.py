"""알림·명령 처리에 쓰는 순수 값 객체."""

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum


class CommandKind(StrEnum):
    STATUS = "status"
    ACCOUNT = "account"
    POSITIONS = "positions"
    PAUSE = "pause"
    STOP = "stop"
    RESUME = "resume"
    LIQUIDATE_ALL = "liquidate_all"
    CONFIRM = "confirm"
    HELP = "help"


class NotificationPriority(IntEnum):
    CRITICAL = 0
    NORMAL = 10
    DIGEST = 20


class InvalidCommand(ValueError):
    """지원하지 않거나 안전한 문법이 아닌 명령이다."""


@dataclass(frozen=True)
class ParsedCommand:
    kind: CommandKind
    argument: str | None = None


@dataclass(frozen=True)
class OperatorIdentity:
    user_id: int
    chat_id: int


@dataclass(frozen=True)
class InboundMessage:
    update_id: int
    user_id: int
    chat_id: int
    chat_type: str
    text: str
    forwarded: bool = False


@dataclass(frozen=True)
class OperationalEvent:
    kind: str
    source_type: str
    source_id: int
    version: str
    payload: dict[str, object]
    occurred_at: datetime


@dataclass(frozen=True)
class RenderedPart:
    index: int
    total: int
    text: str
    parse_mode: None = None
