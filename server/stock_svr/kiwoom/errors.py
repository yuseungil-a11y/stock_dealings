"""키움 API 오류 정의."""
from __future__ import annotations

# errorCodeList 중 재시도 가치가 있는 코드
RATE_LIMIT_CODES = {1700}
RETRYABLE_CODES = {1700, 9999}
# 토큰 만료/인증 실패 계열
AUTH_ERROR_CODES = {8005, 8010, 8030, 40010, 40011}


class KiwoomError(RuntimeError):
    """키움 연동 공통 예외."""


class KiwoomHttpError(KiwoomError):
    def __init__(self, api_id: str, status: int, detail: str = ""):
        self.api_id = api_id
        self.status = status
        super().__init__(f"[{api_id}] HTTP {status} {detail}".strip())


class KiwoomApiError(KiwoomError):
    """HTTP 200 이지만 return_code != 0 인 경우."""

    def __init__(self, api_id: str, return_code: int | None, return_msg: str = ""):
        self.api_id = api_id
        self.return_code = return_code
        self.return_msg = return_msg
        super().__init__(f"[{api_id}] return_code={return_code} {return_msg}".strip())

    @property
    def retryable(self) -> bool:
        return self.return_code in RETRYABLE_CODES

    @property
    def is_auth_error(self) -> bool:
        return self.return_code in AUTH_ERROR_CODES


class RateLimitError(KiwoomApiError):
    """허용 요청 수 초과(1700)."""


class KiwoomAuthError(KiwoomError):
    """토큰 발급/폐기 실패."""


class OrderBlockedError(KiwoomError):
    """주문 게이트가 닫혀 있는데 주문 API 를 호출하려 한 경우(방어선)."""
