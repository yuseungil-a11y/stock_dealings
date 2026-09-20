"""호출 간 최소 간격 유지 + 1700(허용 요청 수 초과) 지수 백오프."""
from __future__ import annotations

import threading
import time


class RateLimiter:
    """요청 사이 최소 간격을 보장하는 간단한 스로틀러 (스레드 안전)."""

    def __init__(self, min_interval_sec: float, max_backoff_sec: float = 30.0):
        self.min_interval = max(0.0, float(min_interval_sec))
        self.max_backoff = float(max_backoff_sec)
        self._lock = threading.Lock()
        self._next_at = 0.0
        self._penalty = 0.0
        self._sleep = time.sleep
        self._clock = time.monotonic

    def acquire(self) -> float:
        """필요한 만큼 대기한다. 실제 대기한 초를 반환."""
        with self._lock:
            now = self._clock()
            wait = self._next_at - now
            if wait < 0:
                wait = 0.0
            self._next_at = max(now, self._next_at) + self.min_interval + self._penalty
        if wait > 0:
            self._sleep(wait)
        return wait

    def penalize(self) -> float:
        """Rate limit 응답을 받았을 때 백오프를 늘린다."""
        with self._lock:
            self._penalty = min(self.max_backoff, self._penalty * 2 if self._penalty else 1.0)
            self._next_at = max(self._next_at, self._clock() + self._penalty)
            return self._penalty

    def relax(self) -> None:
        """정상 응답 시 백오프를 점진적으로 해제."""
        with self._lock:
            if self._penalty:
                self._penalty = 0.0 if self._penalty <= 1.0 else self._penalty / 2

    @property
    def penalty(self) -> float:
        return self._penalty
