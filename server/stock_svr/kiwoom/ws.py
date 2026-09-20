"""키움 WebSocket 실시간 클라이언트.

프로토콜(DEV_SPEC 4절):
  접속 → `{"trnm":"LOGIN","token":"<token>"}` → 응답 return_code==0 확인
       → `{"trnm":"REG","grp_no":"1","refresh":"1","data":[{"item":[...],"type":[...]}]}`
  서버가 `{"trnm":"PING"}` 을 보내면 **같은 메시지를 그대로 되돌려 보낸다**.
  실시간 수신은 `trnm=="REAL"`, `data[].values{필드번호:값}`.

별도 스레드에서 asyncio 이벤트 루프를 돌리며, UI/엔진 스레드를 블로킹하지 않는다.
끊기면 지수 백오프로 자동 재연결한다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Callable

try:
    import websockets
except Exception:  # pragma: no cover - 패키지 미설치 환경 방어
    websockets = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

# 실시간 항목 타입
TYPE_ORDER_EXEC = "00"   # 주문체결
TYPE_BALANCE = "04"      # 잔고
TYPE_MARKET_TIME = "0s"  # 장시작시간
TYPE_STOCK_EXEC = "0B"   # 주식체결
TYPE_VI = "1h"           # VI 발동/해제

# 이 시간(초) 동안 아무 메시지도 오지 않으면 연결이 죽은 것으로 보고 재연결한다 (B11)
RECV_TIMEOUT_SEC = 180


def login_ok(data: dict) -> bool:
    """WS LOGIN 응답이 성공인지 판정 (S-20: return_code == 0 명시 요구)."""
    try:
        return int(str((data or {}).get("return_code")).strip()) == 0
    except (TypeError, ValueError):
        return False


class KiwoomWebSocket:
    """실시간 구독 클라이언트."""

    def __init__(
        self,
        ws_url: str,
        token_provider: Callable[[], str],
        on_real: Callable[[str, str, dict], None] | None = None,
        on_status: Callable[[str, str], None] | None = None,
    ):
        self.ws_url = ws_url
        self._token_provider = token_provider
        self._on_real = on_real
        self._on_status = on_status
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()
        self._stop_async: asyncio.Event | None = None
        self._ws = None
        self.connected = threading.Event()
        self.last_message: str = ""
        self._subs: list[tuple[str, list[str], list[str]]] = []

    # ------------------------------------------------------------------ #
    def subscribe(self, grp_no: str, items: list[str], types: list[str]) -> None:
        """구독 등록(연결 전에 호출하면 접속 후 자동 등록)."""
        self._subs.append((grp_no, list(items), list(types)))
        if self.connected.is_set() and self._loop:
            asyncio.run_coroutine_threadsafe(self._send_reg(grp_no, items, types), self._loop)

    def start(self) -> None:
        if websockets is None:
            self._status("error", "websockets 패키지가 없어 실시간 연결을 사용할 수 없습니다.")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="kiwoom-ws", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        loop, ws = self._loop, self._ws
        if loop is not None and self._stop_async is not None:
            # 백오프 sleep 중이면 즉시 깨운다 (B11)
            loop.call_soon_threadsafe(self._stop_async.set)
        if loop and ws is not None:
            asyncio.run_coroutine_threadsafe(_safe_close(ws), loop)
        if self._thread:
            self._thread.join(timeout=timeout)
        self.connected.clear()

    # ------------------------------------------------------------------ #
    def _status(self, status: str, message: str) -> None:
        self.last_message = message
        if self._on_status:
            try:
                self._on_status(status, message)
            except Exception:  # noqa: BLE001
                log.debug("ws on_status 콜백 실패", exc_info=True)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except Exception:  # noqa: BLE001
            log.exception("WS 스레드 종료(예외)")
        finally:
            try:
                loop.close()
            finally:
                self._loop = None

    async def _main(self) -> None:
        self._stop_async = asyncio.Event()
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._status("warn", "연결 시도 중")
                async with websockets.connect(self.ws_url, ping_interval=None, close_timeout=3) as ws:
                    self._ws = ws
                    await self._login(ws)
                    self.connected.set()
                    backoff = 1.0
                    self._status("ok", "실시간 연결됨")
                    for grp_no, items, types in list(self._subs):
                        await self._send_reg(grp_no, items, types)
                    await self._recv_loop(ws)
            except asyncio.CancelledError:  # pragma: no cover
                break
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set():
                    break
                self._status("error", f"연결 오류: {type(exc).__name__}")
                log.warning("WS 연결 오류(%s) - %.1fs 후 재시도", type(exc).__name__, backoff)
            finally:
                self.connected.clear()
                self._ws = None
            if self._stop.is_set():
                break
            # B11: 백오프 대기 중에도 stop() 이 오면 즉시 깨어난다
            try:
                await asyncio.wait_for(self._stop_async.wait(), timeout=backoff)
                break
            except asyncio.TimeoutError:
                pass
            backoff = min(60.0, backoff * 2)
        self._status("unknown", "실시간 연결 종료")

    async def _login(self, ws) -> None:
        await ws.send(json.dumps({"trnm": "LOGIN", "token": self._token_provider()}))
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
        data = json.loads(raw)
        # S-20: return_code == 0 을 **명시적으로** 요구한다(누락/불명은 실패 처리)
        if not login_ok(data):
            raise RuntimeError(
                f"WS LOGIN 실패 return_code={data.get('return_code')} {data.get('return_msg', '')}")
        log.info("WS LOGIN 성공")

    async def _send_reg(self, grp_no: str, items: list[str], types: list[str]) -> None:
        ws = self._ws
        if ws is None:
            return
        msg = {
            "trnm": "REG",
            "grp_no": str(grp_no),
            "refresh": "1",
            "data": [{"item": items, "type": types}],
        }
        await ws.send(json.dumps(msg))
        log.info("WS 구독 등록 grp=%s types=%s items=%d", grp_no, types, len(items))

    async def _recv_loop(self, ws) -> None:
        while not self._stop.is_set():
            # B11: 무수신 감시 - RECV_TIMEOUT_SEC 동안 아무 메시지도 없으면 재연결
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"{RECV_TIMEOUT_SEC}초간 실시간 메시지 없음 - 재연결") from None
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue
            trnm = data.get("trnm")
            if trnm == "PING":
                # 받은 메시지를 그대로 되돌려 보낸다
                await ws.send(raw if isinstance(raw, str) else json.dumps(data))
                continue
            if trnm == "REAL":
                self._dispatch(data)
            elif trnm in ("LOGIN", "REG", "REMOVE"):
                if str(data.get("return_code", "0")) not in ("0", "None"):
                    log.warning("WS %s 응답 오류: %s", trnm, data.get("return_msg"))

    def _dispatch(self, data: dict) -> None:
        if self._on_real is None:
            return
        for entry in data.get("data") or []:
            if not isinstance(entry, dict):
                continue
            rtype = str(entry.get("type", ""))
            item = str(entry.get("item", ""))
            values = entry.get("values") or {}
            if not isinstance(values, dict):
                continue
            try:
                self._on_real(rtype, item, values)
            except Exception:  # noqa: BLE001 - 한 건 실패가 수신루프를 죽이지 않게
                log.exception("실시간 데이터 처리 실패 type=%s", rtype)


async def _safe_close(ws) -> None:
    try:
        await ws.close()
    except Exception:  # noqa: BLE001
        pass
