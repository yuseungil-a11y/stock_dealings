"""접근토큰 발급(au10001)/폐기(au10002).

토큰은 **메모리에만** 보관하고 로그/DB/화면 어디에도 남기지 않는다.
"""
from __future__ import annotations

import datetime as _dt
import logging
import threading

import httpx

from ..config import KiwoomConfig
from .errors import KiwoomAuthError
from .parse import to_datetime

log = logging.getLogger(__name__)

TOKEN_API = "au10001"
REVOKE_API = "au10002"
# 만료 이 시간 전이면 미리 재발급
REFRESH_MARGIN = _dt.timedelta(minutes=10)


class TokenManager:
    """접근토큰 수명 관리. 스레드 안전."""

    def __init__(self, cfg: KiwoomConfig, env: str, http_timeout: float | None = None):
        self.cfg = cfg
        self.env = env
        self.timeout = http_timeout if http_timeout is not None else cfg.http_timeout_sec
        self._lock = threading.RLock()
        self._token: str | None = None
        self._expires_at: _dt.datetime | None = None
        self.issued_at: _dt.datetime | None = None

    # ------------------------------------------------------------------ #
    @property
    def has_token(self) -> bool:
        return bool(self._token)

    @property
    def expires_at(self) -> _dt.datetime | None:
        return self._expires_at

    def masked(self) -> str:
        # S-17: 접두 문자·길이도 노출하지 않는다(토큰 식별 단서 제거)
        return "***" if self._token else "(none)"

    def _expired(self) -> bool:
        if not self._token:
            return True
        if not self._expires_at:
            return False
        return _dt.datetime.now() >= (self._expires_at - REFRESH_MARGIN)

    def get_token(self, force: bool = False) -> str:
        with self._lock:
            if force or self._expired():
                self._issue()
            assert self._token
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = None

    # ------------------------------------------------------------------ #
    def _issue(self) -> None:
        appkey, secretkey = self.cfg.read_keys(self.env)
        url = self.cfg.domain(self.env) + "/oauth2/token"
        body = {"grant_type": "client_credentials", "appkey": appkey, "secretkey": secretkey}
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, json=body, headers={"content-type": "application/json;charset=UTF-8"})
        except httpx.HTTPError as exc:
            raise KiwoomAuthError(f"토큰 발급 통신 실패: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise KiwoomAuthError(f"토큰 발급 HTTP {resp.status_code}")
        data = resp.json()
        rc = data.get("return_code")
        if rc not in (0, "0", None):
            raise KiwoomAuthError(f"토큰 발급 실패 return_code={rc} {data.get('return_msg', '')}")
        token = data.get("token")
        if not token:
            raise KiwoomAuthError("토큰 발급 응답에 token 이 없습니다.")
        self._token = token
        self.issued_at = _dt.datetime.now()
        expires = to_datetime(data.get("expires_dt"))
        if expires is None:
            # B10: 만료시각을 못 읽으면 보수적으로 발급 + 23시간으로 둔다
            expires = self.issued_at + _dt.timedelta(hours=23)
            log.warning("expires_dt 파싱 실패 - 만료를 발급+23h 로 보수 설정")
        self._expires_at = expires
        log.info("접근토큰 발급 완료 (env=%s, token=%s, expires=%s)",
                 self.env, self.masked(), self._expires_at)

    def revoke(self) -> bool:
        """토큰 폐기. 실패해도 예외를 밖으로 던지지 않는다(종료 경로에서 호출)."""
        with self._lock:
            token = self._token
            self._token = None
            self._expires_at = None
        if not token:
            return False
        try:
            appkey, secretkey = self.cfg.read_keys(self.env)
            url = self.cfg.domain(self.env) + "/oauth2/revoke"
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, json={"appkey": appkey, "secretkey": secretkey, "token": token},
                                   headers={"content-type": "application/json;charset=UTF-8"})
            ok = resp.status_code == 200
            log.info("접근토큰 폐기 %s (HTTP %s)", "성공" if ok else "실패", resp.status_code)
            return ok
        except Exception as exc:  # noqa: BLE001 - 종료 경로 보호
            log.warning("접근토큰 폐기 중 오류: %s", type(exc).__name__)
            return False
