"""Telegram persistence boundary validation and canonical JSON limits."""

import json
import re
from datetime import datetime
from typing import Any

from app.domain.notifications.models import CommandKind

HASH_RE = re.compile(r"^[0-9a-f]{64}$")
VERSIONED_HASH_RE = re.compile(r"^v1:[0-9a-f]{64}$")
MAX_JSON_BYTES = 64 * 1024
MAX_JSON_DEPTH = 12
MAX_JSON_ITEMS = 2_000
MAX_DELIVERY_BODY_CHARS = 4096
MAX_DELIVERY_PARTS = 64
MAX_DELIVERY_TOTAL_BYTES = 256 * 1024
SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware datetime required")
    return value


def exact_nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be an exact non-negative int")
    return value


def positive_int(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive int")
    return value


def hash64(value: str, field: str) -> str:
    if not HASH_RE.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 hex digest")
    return value


def external_hash(value: str, field: str) -> str:
    """Only the current keyed external identifier format may be persisted."""
    if not VERSIONED_HASH_RE.fullmatch(value):
        raise ValueError(f"{field} must be a versioned HMAC-SHA-256 digest")
    return value


def command(value: str) -> str:
    if value not in {item.value for item in CommandKind}:
        raise ValueError("unsupported command")
    return value


def identifier(value: str, field: str, maximum: int) -> str:
    if not value or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{field} length is invalid")
    return value


def safe_identifier(value: str, field: str) -> str:
    if not SAFE_IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{field} contains unsafe characters")
    return value


def canonical_json(value: Any) -> str:
    count = 0

    def walk(item: Any, depth: int) -> None:
        nonlocal count
        if depth > MAX_JSON_DEPTH:
            raise ValueError("JSON nesting is too deep")
        count += 1
        if count > MAX_JSON_ITEMS:
            raise ValueError("JSON contains too many items")
        if isinstance(item, dict):
            if any(type(key) is not str for key in item):
                raise ValueError("JSON keys must be strings")
            for child in item.values():
                walk(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child, depth + 1)

    walk(value, 0)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("JSON exceeds persistence byte limit")
    return encoded


def delivery_body(value: str) -> str:
    if type(value) is not str or len(value) > MAX_DELIVERY_BODY_CHARS:
        raise ValueError("delivery body exceeds Telegram 4096-character contract")
    return value
