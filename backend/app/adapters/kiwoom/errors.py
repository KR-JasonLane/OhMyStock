class BrokerError(Exception):
    """브로커 어댑터 공통 베이스 — 호출자는 이 타입 하나로 브로커 장애를 처리한다."""


class AuthError(BrokerError):
    pass


class RateLimitError(BrokerError):
    pass


class ApiError(BrokerError):
    def __init__(self, return_code: int, return_msg: str, api_id: str | None = None):
        self.return_code = return_code
        self.return_msg = return_msg
        self.api_id = api_id
        super().__init__(f"kiwoom api error [{api_id}] {return_code}: {return_msg}")
