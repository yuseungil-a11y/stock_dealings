"""테스트용 가짜 키움 클라이언트.

실제 주문 API 는 개발/테스트 중 절대 호출하지 않는다(DEV_SPEC 2-1).
주문 경로 테스트는 반드시 이 fake 클라이언트로만 수행한다.
"""
from __future__ import annotations

import contextlib
import threading
from typing import Any, Callable, Iterator

from .errors import OrderBlockedError
from .rest import ORDER_API_IDS


class FakeRest:
    """`KiwoomRest` 와 같은 인터페이스를 가진 가짜 클라이언트.

    * 호출 기록을 `calls` 에 남긴다.
    * 주문 API 는 실제 클라이언트와 동일하게 `unlock_orders()` 밖에서 호출하면 예외.
    """

    def __init__(self, responses: dict[str, Any] | None = None,
                 handler: Callable[[str, dict], dict] | None = None,
                 env: str | None = None):
        # env 가 None 이면 Executor 의 env↔trading_mode 일치 검증을 건너뛴다(기존 테스트 호환)
        self.env = env
        self.responses = dict(responses or {})
        self.handler = handler
        self.calls: list[tuple[str, dict]] = []
        self.order_calls: list[tuple[str, dict]] = []
        self._unlocked = threading.local()
        self._seq = 0
        self.raise_on: dict[str, Exception] = {}
        self.last_error = None

    # ------------------------------------------------------------------ #
    @property
    def orders_unlocked(self) -> bool:
        return bool(getattr(self._unlocked, "value", False))

    @contextlib.contextmanager
    def unlock_orders(self) -> Iterator[None]:
        prev = self.orders_unlocked
        self._unlocked.value = True
        try:
            yield
        finally:
            self._unlocked.value = prev

    # ------------------------------------------------------------------ #
    def call(self, api_id: str, body: dict | None = None, **kw):
        body = dict(body or {})
        if api_id in ORDER_API_IDS and not self.orders_unlocked:
            raise OrderBlockedError(f"주문 API({api_id}) 차단 - 게이트 미통과")
        self.calls.append((api_id, body))
        if api_id in ORDER_API_IDS:
            self.order_calls.append((api_id, body))
        if api_id in self.raise_on:
            raise self.raise_on[api_id]
        if self.handler is not None:
            data = self.handler(api_id, body)
        elif api_id in self.responses:
            data = self.responses[api_id]
        elif api_id in ORDER_API_IDS:
            self._seq += 1
            data = {"return_code": 0, "return_msg": "정상", "ord_no": f"FAKE{self._seq:05d}",
                    "dmst_stex_tp": body.get("dmst_stex_tp", "KRX")}
        else:
            data = {"return_code": 0, "return_msg": "정상"}
        return data, {"cont-yn": "N"}

    def call_paged(self, api_id: str, body: dict | None = None, *, with_meta: bool = False,
                   **kw):
        """실물과 같은 시그니처. `truncated` 는 `self.paged_truncated` 로 흉내낼 수 있다(R-14)."""
        data, _ = self.call(api_id, body)
        if with_meta:
            return [data], {"truncated": bool(getattr(self, "paged_truncated", False))}
        return [data]

    def close(self) -> None:
        pass

    # ------------------------------------------------------------------ #
    @property
    def order_call_count(self) -> int:
        return len(self.order_calls)

    def called(self, api_id: str) -> int:
        return sum(1 for a, _ in self.calls if a == api_id)
