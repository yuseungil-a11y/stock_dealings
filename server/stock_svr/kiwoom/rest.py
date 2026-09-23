"""키움 REST TR 호출기.

모든 TR 은 POST + JSON. 헤더에 `authorization: Bearer <token>`, `api-id`.
연속조회는 응답 헤더 `cont-yn=Y` 일 때 `next-key` 를 다음 요청에 넣어 반복한다.

**안전장치**: 주문 계열 API(`ORDER_API_IDS`)는 기본적으로 차단되며,
`unlock_orders()` 컨텍스트 안에서만 호출할 수 있다. 이 컨텍스트는
`engine.executor.Executor` 의 주문 게이트를 통과한 경로에서만 열린다.
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Any, Callable, Iterator

import httpx

from ..config import KiwoomConfig
from ..util import mask_text
from .auth import TokenManager
from .errors import KiwoomApiError, KiwoomError, KiwoomHttpError, OrderBlockedError, RateLimitError
from .ratelimit import RateLimiter

log = logging.getLogger(__name__)

# 실주문/정정/취소 계열 - 개발·테스트 중 실서버 호출 금지 (DEV_SPEC 2-1)
ORDER_API_IDS = frozenset({
    "kt10000", "kt10001", "kt10002", "kt10003",
    "kt10006", "kt10007", "kt10008", "kt10009",
    "kt50000", "kt50001", "kt50002", "kt50003",
})

# 엔드포인트 경로 매핑 (api_id -> path)
API_PATHS: dict[str, str] = {
    # 계좌
    "ka00001": "/api/dostk/acnt",
    "kt00001": "/api/dostk/acnt",
    "kt00004": "/api/dostk/acnt",
    "kt00005": "/api/dostk/acnt",
    "kt00015": "/api/dostk/acnt",
    "kt00018": "/api/dostk/acnt",
    "ka10075": "/api/dostk/acnt",
    "ka10076": "/api/dostk/acnt",
    "ka10077": "/api/dostk/acnt",
    "ka10170": "/api/dostk/acnt",
    # 종목/시세
    "ka10001": "/api/dostk/stkinfo",
    "ka10099": "/api/dostk/stkinfo",
    "ka10081": "/api/dostk/chart",
    # 순위
    "ka10023": "/api/dostk/rkinfo",
    "ka10027": "/api/dostk/rkinfo",
    # 테마 (읽기 전용 - claude_trend_scan)
    "ka90001": "/api/dostk/thme",
    "ka90002": "/api/dostk/thme",
    # 주문
    "kt10000": "/api/dostk/ordr",
    "kt10001": "/api/dostk/ordr",
    "kt10002": "/api/dostk/ordr",
    "kt10003": "/api/dostk/ordr",
}

MAX_RETRY = 3


class KiwoomRest:
    """TR 호출 클라이언트 (스레드 안전)."""

    def __init__(
        self,
        cfg: KiwoomConfig,
        env: str,
        token_manager: TokenManager,
        on_call: Callable[[str, int | None, int | None, str, int], None] | None = None,
    ):
        self.cfg = cfg
        self.env = env
        self.tokens = token_manager
        self.limiter = RateLimiter(cfg.min_interval(env))
        self._on_call = on_call
        self._client = httpx.Client(timeout=cfg.http_timeout_sec)
        self._lock = threading.RLock()
        self._orders_unlocked = threading.local()
        self.last_error: str | None = None
        self.last_ok_at: float | None = None

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._client.close()

    # -- 주문 잠금 ------------------------------------------------------ #
    @property
    def orders_unlocked(self) -> bool:
        return bool(getattr(self._orders_unlocked, "value", False))

    @contextlib.contextmanager
    def unlock_orders(self) -> Iterator[None]:
        """주문 API 호출 허용 구간. Executor 게이트 통과 시에만 사용."""
        prev = self.orders_unlocked
        self._orders_unlocked.value = True
        try:
            yield
        finally:
            self._orders_unlocked.value = prev

    # ------------------------------------------------------------------ #
    def path_for(self, api_id: str) -> str:
        try:
            return API_PATHS[api_id]
        except KeyError:
            raise KiwoomError(
                f"알 수 없는 api-id: {api_id} (API_PATHS 에 경로를 등록하세요)") from None

    def call(
        self,
        api_id: str,
        body: dict[str, Any] | None = None,
        *,
        path: str | None = None,
        cont_yn: str | None = None,
        next_key: str | None = None,
        no_retry: bool | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """단건 호출. (응답 body, 응답 header) 반환.

        **주문 계열 API 는 어떤 경우에도 재시도하지 않는다(S-01).** 타임아웃·HTTP 5xx/429·
        `return_code=1700`·401 모두 1회 POST 후 즉시 예외를 올린다. 중복 체결 위험이
        재시도 이득보다 크기 때문이며, 실제 접수 여부는 ka10075/ka10076 동기화로 확정한다.
        """
        if api_id in ORDER_API_IDS and not self.orders_unlocked:
            raise OrderBlockedError(
                f"주문 API({api_id}) 는 Executor 의 주문 게이트를 통과한 경우에만 호출할 수 있습니다."
            )
        if no_retry is None:
            no_retry = api_id in ORDER_API_IDS
        max_retry = 0 if no_retry else MAX_RETRY
        url = self.cfg.domain(self.env) + (path or self.path_for(api_id))
        payload = dict(body or {})

        attempt = 0
        while True:
            attempt += 1
            headers = {
                "content-type": "application/json;charset=UTF-8",
                "authorization": f"Bearer {self.tokens.get_token()}",
                "api-id": api_id,
            }
            if cont_yn:
                headers["cont-yn"] = cont_yn
            if next_key:
                headers["next-key"] = next_key

            self.limiter.acquire()
            started = time.perf_counter()
            status: int | None = None
            rc: int | None = None
            rmsg = ""
            try:
                resp = self._client.post(url, json=payload, headers=headers)
                status = resp.status_code
                elapsed = int((time.perf_counter() - started) * 1000)
                if status != 200:
                    rmsg = f"HTTP {status}"
                    self._record(api_id, status, None, rmsg, elapsed)
                    if status in (429, 500, 502, 503, 504) and attempt <= max_retry:
                        self.limiter.penalize()
                        continue
                    if status in (401, 403) and attempt <= max_retry:
                        self.tokens.invalidate()
                        continue
                    self.last_error = rmsg
                    raise KiwoomHttpError(api_id, status)
                data = resp.json()
                rc = _as_int(data.get("return_code"))
                rmsg = str(data.get("return_msg", ""))[:200]
                self._record(api_id, status, rc, rmsg, elapsed)
                if rc not in (0, None):
                    err = RateLimitError(api_id, rc, rmsg) if rc == 1700 else KiwoomApiError(api_id, rc, rmsg)
                    if rc == 1700 and attempt <= max_retry:
                        back = self.limiter.penalize()
                        log.warning("[%s] rate limit(1700) - %.1fs 백오프 후 재시도", api_id, back)
                        continue
                    if err.is_auth_error and attempt <= max_retry:
                        log.warning("[%s] 인증 오류(%s) - 토큰 재발급 후 재시도", api_id, rc)
                        self.tokens.invalidate()
                        continue
                    self.last_error = str(err)
                    raise err
                self.limiter.relax()
                self.last_error = None
                self.last_ok_at = time.time()
                return data, {k.lower(): v for k, v in resp.headers.items()}
            except httpx.HTTPError as exc:
                elapsed = int((time.perf_counter() - started) * 1000)
                rmsg = type(exc).__name__
                self._record(api_id, status, None, rmsg, elapsed)
                if attempt <= max_retry:
                    time.sleep(min(8.0, 0.5 * (2 ** (attempt - 1))))
                    continue
                self.last_error = rmsg
                raise KiwoomHttpError(api_id, status or 0, rmsg) from exc

    def call_paged(
        self,
        api_id: str,
        body: dict[str, Any] | None = None,
        *,
        path: str | None = None,
        max_pages: int = 10,
        with_meta: bool = False,
    ):
        """연속조회(cont-yn/next-key)를 따라가며 모든 페이지를 모은다.

        `with_meta=True` 면 `(pages, {"truncated": bool})` 를 돌려준다.
        `truncated=True` 는 **max_pages 상한 때문에 남은 페이지를 못 받았다**는 뜻이므로,
        호출부는 이 결과를 '전체 목록'으로 취급하면 안 된다 (R-14).
        """
        pages: list[dict[str, Any]] = []
        cont_yn: str | None = None
        next_key: str | None = None
        truncated = False
        for _ in range(max(1, max_pages)):
            data, headers = self.call(api_id, body, path=path, cont_yn=cont_yn, next_key=next_key)
            pages.append(data)
            if headers.get("cont-yn", "N").upper() != "Y":
                break
            next_key = headers.get("next-key") or ""
            cont_yn = "Y"
            if not next_key:
                break
        else:
            # break 없이 상한까지 돌았다 = 마지막 응답이 아직 cont-yn=Y 였다 → 잘렸다
            truncated = True
        if not with_meta:
            return pages
        return pages, {"truncated": truncated}

    # ------------------------------------------------------------------ #
    def _record(self, api_id: str, status: int | None, rc: int | None, msg: str, elapsed_ms: int) -> None:
        if self._on_call is None:
            return
        try:
            self._on_call(api_id, status, rc, mask_text(msg or "")[:200], elapsed_ms)
        except Exception:  # noqa: BLE001 - 로깅 실패가 호출을 막지 않도록
            log.debug("api_call_log 기록 실패", exc_info=True)


def _as_int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
