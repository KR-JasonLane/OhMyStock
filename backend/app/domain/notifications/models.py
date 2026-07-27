"""알림·명령 처리에 쓰는 순수 값 객체."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum
from math import isfinite
from types import MappingProxyType


class CommandKind(StrEnum):
    STATUS = "status"
    ACCOUNT = "account"
    POSITIONS = "positions"
    ANALYSIS = "analysis"
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
    argument_hash: str | None = None


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
    forwarded: bool

    def __post_init__(self) -> None:
        if type(self.forwarded) is not bool:
            raise ValueError("forwarded must be a bool")


@dataclass(frozen=True)
class OperationalEvent:
    kind: str
    source_type: str
    source_id: int
    version: str
    payload: Mapping[str, object]
    occurred_at: datetime

    def __post_init__(self) -> None:
        if not self.kind or not self.source_type or not self.version:
            raise ValueError("event identifiers must be non-empty")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        object.__setattr__(self, "payload", _freeze_json_snapshot(self.payload))

    def payload_for_storage(self) -> dict[str, object]:
        """영속 계층에 넘길 독립적인 JSON-호환 mutable 복사본을 반환한다."""
        payload = _json_mutable_copy(self.payload)
        assert isinstance(payload, dict)
        return payload


@dataclass(frozen=True)
class RenderedPart:
    index: int
    total: int
    text: str
    parse_mode: None = None

    def __post_init__(self) -> None:
        if self.total < 1 or not 1 <= self.index <= self.total:
            raise ValueError("rendered part index must be within total")


def _freeze_json_snapshot(payload: Mapping[str, object]) -> Mapping[str, object]:
    """입력 변경과 비 JSON 값을 차단한 뒤 깊게 동결한 JSON 스냅샷을 만든다."""
    snapshot = _json_mutable_copy(payload)
    if not isinstance(snapshot, dict):
        raise ValueError("payload must be a JSON object")
    return _freeze_json_value(snapshot)


def _freeze_json_value(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _json_mutable_copy(value: object) -> object:
    if isinstance(value, Mapping):
        copied: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("payload object keys must be str")
            copied[key] = _json_mutable_copy(item)
        return copied
    if isinstance(value, (list, tuple)):
        return [_json_mutable_copy(item) for item in value]
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float and isfinite(value):
        return value
    raise ValueError("payload must contain JSON-compatible values")
