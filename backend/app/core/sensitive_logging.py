"""시크릿 URL을 쓰는 외부 HTTP client의 access log 안전 기본값."""

import logging


def configure_sensitive_http_logging() -> None:
    """HTTP access trace를 끄되 warning/error 관측성은 유지한다."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
