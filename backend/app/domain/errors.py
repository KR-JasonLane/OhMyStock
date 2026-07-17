"""브로커 포트 계약의 에러 계층. 포트 소비자는 이 타입들만 안다."""


class BrokerError(Exception):
    """브로커 어댑터 공통 베이스 — 호출자는 이 타입 하나로 브로커 장애를 처리한다."""


class AuthError(BrokerError):
    pass


class RateLimitError(BrokerError):
    pass


class ApiError(BrokerError):
    """필드명(return_code/return_msg/api_id)은 현재 키움 원본 어휘를 반영한 상태 —
    두 번째 브로커 도입 시 벤더 중립 이름으로 추상화 필요."""

    def __init__(self, return_code: int, return_msg: str, api_id: str | None = None):
        self.return_code = return_code
        self.return_msg = return_msg
        self.api_id = api_id
        super().__init__(f"kiwoom api error [{api_id}] {return_code}: {return_msg}")
