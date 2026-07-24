"""공식 Telegram Bot API 직접 호출 어댑터."""

from app.adapters.telegram.client import (
    TelegramAuthenticationError,
    TelegramClient,
    TelegramPermanentError,
    TelegramRateLimited,
    TelegramTemporaryError,
)

__all__ = [
    "TelegramAuthenticationError",
    "TelegramClient",
    "TelegramPermanentError",
    "TelegramRateLimited",
    "TelegramTemporaryError",
]
