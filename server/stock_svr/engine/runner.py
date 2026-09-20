"""백그라운드 엔진 루프.

시작 → 설정 로드 → DB → 토큰 → 계좌 확인/등록 → 종목마스터(하루 1회) → WS 구독 → 주기 루프.
한 서비스의 예외가 엔진 전체를 죽이지 않도록 각 단계를 격리하고, 네트워크/토큰 오류는 재시도한다.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from ..algo import registry
from ..algo.base import Signal
from ..algo.claude_advisor import CODE as CLAUDE_CODE
from ..config import AppConfig
from ..db import Database
from ..kiwoom.auth import TokenManager
from ..kiwoom.rest import KiwoomRest
from ..kiwoom.ws import (
    TYPE_BALANCE,
    TYPE_MARKET_TIME,
    TYPE_ORDER_EXEC,
    KiwoomWebSocket,
)
from ..services.housekeeping import HousekeepingService
from ..services.sync_account import AccountService
from ..services.sync_market import MarketService
from ..services.sync_orders import OrderSyncService
from ..util import is_market_open, mask_account_no, now_kst
from .context import EngineContext, OrderGateState, gate_widened
from .executor import DEFAULT_COOLDOWN_SEC, Executor

log = logging.getLogger(__name__)

HEARTBEAT_SEC = 10
ACCOUNT_SYNC_OPEN_SEC = 60
ACCOUNT_SYNC_CLOSED_SEC = 600
ORDER_SYNC_OPEN_SEC = 30
ORDER_SYNC_CLOSED_SEC = 900
TICK_SEC = 1.0

# 자동거래(알고리즘 평가·주문) 상태를 웹 관제에 노출하는 server_status 컴포넌트
AUTO_COMPONENT = "auto_trading"
AUTO_TEXT_STOPPED = "중지"
AUTO_TEXT_OBSERVE = "실행중 · 관찰(신호만, 주문 미전송)"
AUTO_TEXT_ORDER_ON = "실행중 · 주문 전송 ON"

# 주문번호 없이 SENT 로 남은 주문을 FAILED 로 정리하기까지의 시간(분) (R-07)
UNKNOWN_ORDER_EXPIRE_MIN = 10

# risk_guard 가 빠진 사이클을 설명하는 고정 문구 (R-01)
NO_GUARD_REASON = "risk_guard 미탑재 - 전 주문 차단"


def annotate_open_orders(db, account_id: int, orders: list[dict]) -> list[dict]:
    """ka10075 미체결 목록에 '우리 알고리즘 주문인지'를 덧붙인다 (R-04).

    ka10075 는 **계좌 전체** 미체결(수동 HTS 주문 포함)을 돌려주므로,
    UI 가 기본적으로 우리 주문만 선택할 수 있도록 근거를 만들어 준다.
    """
    out: list[dict] = []
    for o in orders:
        row = dict(o)
        algo = None
        try:
            algo = db.order_algo_code(account_id, row.get("ord_no"))
        except Exception:  # noqa: BLE001 - 조회 실패는 '외부 주문'으로 보수적으로 본다
            log.debug("주문 출처 조회 실패: %s", row.get("ord_no"), exc_info=True)
        algo = (algo or "").strip() or None
        row["algo_code"] = algo
        row["is_ours"] = bool(algo) and algo != "manual_cancel"
        out.append(row)
    return out


@dataclass
class EngineStatus:
    """UI 상태바가 읽는 스냅샷."""

    running: bool = False
    db: tuple[str, str] = ("unknown", "")
    kiwoom_rest: tuple[str, str] = ("unknown", "")
    kiwoom_ws: tuple[str, str] = ("unknown", "")
    market: tuple[str, str] = ("unknown", "")
    mode: str = "REAL"
    order_allowed: bool = False
    gate_text: str = ""
    account_no_masked: str = "(미확인)"
    account_id: int | None = None
    run_id: int | None = None
    last_cycle_at: str = ""
    last_error: str = ""
    cycles: int = 0
    signals: int = 0
    orders_sent: int = 0
    auto_trading: bool = False
    auto_status: tuple[str, str] = ("unknown", AUTO_TEXT_STOPPED)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.running, "db": self.db, "kiwoom_rest": self.kiwoom_rest,
                "kiwoom_ws": self.kiwoom_ws, "market": self.market, "mode": self.mode,
                "order_allowed": self.order_allowed, "gate_text": self.gate_text,
                "account_no_masked": self.account_no_masked, "account_id": self.account_id,
                "run_id": self.run_id, "last_cycle_at": self.last_cycle_at,
                "last_error": self.last_error, "cycles": self.cycles,
                "signals": self.signals, "orders_sent": self.orders_sent,
                "auto_trading": self.auto_trading, "auto_status": self.auto_status,
            }

    def set(self, **kw) -> None:
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)


class Engine:
    """엔진 스레드 관리자."""

    def __init__(self, cfg: AppConfig, db: Database, on_status: Callable[[], None] | None = None):
        self.cfg = cfg
        self.db = db
        self.status = EngineStatus()
        self._on_status = on_status
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self.env = "real"
        self.tokens: TokenManager | None = None
        self.rest: KiwoomRest | None = None
        self.ws: KiwoomWebSocket | None = None
        self.account_id: int | None = None
        self.account_no: str | None = None
        self.market: MarketService | None = None
        self.accounts: AccountService | None = None
        self.orders: OrderSyncService | None = None
        self.house: HousekeepingService | None = None
        self.executor: Executor | None = None
        self.run_id: int | None = None
        self._master_synced_date = None

        # --- 자동거래(알고리즘 평가·주문) 스위치 --------------------- #
        # 엔진(조회·동기화)과 분리된 개념이며 **기동 시 항상 중지 상태**로 시작한다.
        self._auto_trading = threading.Event()
        self._reset_eval = False
        self.auto_trading_started_at = None
        self.auto_trading_by: str = ""
        # 마지막 시작 거부 사유(R-11). UI 가 사용자에게 보여준다.
        self.start_refused_reason: str = ""
        # WS `0s` 장운영구분(215) 최신값 - None 이면 미수신
        self._market_code: str | None = None

    # ================================================================== #
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ================================================================== #
    # 자동거래 제어 (주문 게이트와는 별개의 선행 조건)
    # ================================================================== #
    @property
    def auto_trading_active(self) -> bool:
        return self._auto_trading.is_set()

    def current_gate(self) -> OrderGateState:
        try:
            return OrderGateState.from_settings(self.db.get_settings())
        except Exception:  # noqa: BLE001 - 조회 실패 시 보수적으로 닫힌 게이트
            return OrderGateState()

    def start_auto_trading(self, by: str = "UI 버튼",
                           shown_gate: OrderGateState | None = None) -> bool:
        """자동거래 시작. 이미 실행 중이면 False.

        `shown_gate` 는 확인창에 표시했던 게이트다. 확인창을 띄운 사이 게이트가 **더 열렸으면**
        (OFF→ON, mock→real) 사용자가 승인한 내용과 다르므로 시작을 거부한다 (R-11).
        """
        if self._auto_trading.is_set():
            return False
        gate = self.current_gate()
        if shown_gate is not None and gate_widened(shown_gate, gate):
            reason = (f"확인창 표시 이후 주문 게이트가 더 열렸습니다 "
                      f"({shown_gate.describe()} → {gate.describe()}) - 시작 거부, 재확인 필요")
            log.error("%s", reason)
            self.start_refused_reason = reason
            try:
                self.db.log_event("ERROR", "algo", f"자동거래 시작 거부: {reason}")
            except Exception:  # noqa: BLE001
                pass
            return False
        self.start_refused_reason = ""
        self._auto_trading.set()
        self.auto_trading_started_at = now_kst()
        self.auto_trading_by = by
        self._reset_eval = True
        log.warning("자동거래 시작 (요청: %s, 주문게이트 %s)", by, gate.describe())
        self._publish_auto_status()
        return True

    def stop_auto_trading(self, by: str = "UI 버튼", reason: str = "") -> int:
        """자동거래 중지. 미체결 주문 건수를 반환(자동 취소하지 않음)."""
        was_active = self._auto_trading.is_set()
        self._auto_trading.clear()
        open_cnt = self.count_open_orders()
        if was_active:
            log.warning("자동거래 중지 (요청: %s%s) - 미체결 주문 %d건은 자동 취소하지 않습니다",
                        by, f", 사유: {reason}" if reason else "", open_cnt)
        self._publish_auto_status()
        return open_cnt

    def list_open_orders(self) -> list[dict]:
        """ka10075 **계좌 전체** 미체결 목록 + 우리 알고리즘 주문 여부 (R-04)."""
        if not (self.orders and self.account_id):
            raise RuntimeError("엔진이 준비되지 않았습니다")
        return annotate_open_orders(self.db, self.account_id, self.orders.fetch_open_orders())

    def cancel_all_open_orders(self, by: str = "UI 버튼",
                               only_ord_nos: list[str] | set[str] | None = None,
                               targets: list[dict] | None = None) -> dict:
        """긴급: 미체결 주문(ka10075)을 조회해 순차 취소(kt10003). 재시도하지 않는다(S-15).

        `only_ord_nos` 를 주면 그 주문번호만 취소한다(UI 가 선택한 목록 - R-04).
        루프 도중 주문 게이트가 닫히면 즉시 중단한다.
        """
        out = {"total": 0, "ok": 0, "failed": 0, "skipped": 0, "errors": []}
        if not (self.rest and self.executor and self.account_id):
            out["errors"].append("엔진이 준비되지 않았습니다")
            return out
        if targets is None:
            try:
                targets = self.list_open_orders()
            except Exception as exc:  # noqa: BLE001
                out["errors"].append(f"미체결 조회 실패: {type(exc).__name__}")
                log.error("긴급 취소 - 미체결 조회 실패: %s", exc)
                return out
        if only_ord_nos is not None:
            keep = {str(n) for n in only_ord_nos}
            targets = [o for o in targets if str(o.get("ord_no")) in keep]
        out["total"] = len(targets)
        log.warning("긴급 미체결 취소 시작 (요청: %s, 대상 %d건)", by, len(targets))
        ctx = self.build_context()
        for o in targets:
            # 매 취소 직전 게이트 재확인 — 도중에 닫히면 남은 건은 보내지 않는다(R-04)
            if not self.current_gate().can_send_order:
                out["skipped"] = out["total"] - (out["ok"] + out["failed"])
                out["errors"].append("주문 게이트가 닫혀 남은 취소를 중단했습니다")
                log.warning("긴급 취소 중단: 주문 게이트가 닫힘 (남은 %d건)", out["skipped"])
                break
            res = self.executor.submit_cancel(
                ctx, o["ord_no"], o["stk_cd"], o["oso_qty"], o.get("exchange") or "KRX",
                reason=f"긴급 미체결 취소({by})")
            if res.sent:
                out["ok"] += 1
            else:
                out["failed"] += 1
                if res.error or res.blocked_reason:
                    out["errors"].append(f"{o['ord_no']}: {res.error or res.blocked_reason}")
        self.db.log_event("WARN", "order",
                          f"긴급 미체결 취소({by}): 대상 {out['total']}건 성공 {out['ok']}건 "
                          f"실패 {out['failed']}건 중단 {out['skipped']}건")
        return out

    def count_open_orders(self) -> int:
        if not self.account_id:
            return 0
        try:
            return self.db.count_open_orders(self.account_id)
        except Exception:  # noqa: BLE001
            return 0

    def _publish_auto_status(self) -> None:
        active = self._auto_trading.is_set()
        if not active:
            status, text = "unknown", AUTO_TEXT_STOPPED
        else:
            gate = self.current_gate()
            if gate.can_send_order:
                status = "warn"
                text = f"{AUTO_TEXT_ORDER_ON} ({gate.trading_mode.upper()})"
            else:
                status, text = "ok", AUTO_TEXT_OBSERVE
        self.status.set(auto_trading=active, auto_status=(status, text))
        try:
            self.db.set_status(AUTO_COMPONENT, status, text)
        except Exception:  # noqa: BLE001
            pass
        self._notify()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="stock-svr-engine", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._thread = None

    def request_stop(self) -> None:
        self._stop.set()

    # ================================================================== #
    def _notify(self) -> None:
        if self._on_status:
            try:
                self._on_status()
            except Exception:  # noqa: BLE001
                pass

    def _set_status(self, component: str, status: str, message: str = "") -> None:
        self.status.set(**{component: (status, message)})
        try:
            self.db.set_status(component, status, message)
        except Exception:  # noqa: BLE001
            pass
        self._notify()

    # ================================================================== #
    def _run(self) -> None:
        self.status.set(running=True, last_error="")
        try:
            self._startup()
            self._loop()
        except Exception as exc:  # noqa: BLE001
            log.exception("엔진 치명적 오류")
            self.status.set(last_error=f"{type(exc).__name__}: {exc}"[:200])
            self._set_status("kiwoom_rest", "error", f"{type(exc).__name__}")
        finally:
            self._shutdown()
            self.status.set(running=False)
            self._notify()

    # -- 기동 ---------------------------------------------------------- #
    def _startup(self) -> None:
        settings = self.db.get_settings()
        gate = OrderGateState.from_settings(settings)
        self.env = "mock" if gate.trading_mode == "mock" else "real"
        self.status.set(mode=gate.trading_mode.upper(), order_allowed=gate.can_send_order,
                        gate_text=gate.describe())
        log.info("엔진 시작 (env=%s, 주문게이트 %s)", self.env, gate.describe())

        if self.db.ping():
            self._set_status("db", "ok", f"{self.cfg.db.host}:{self.cfg.db.port}/{self.cfg.db.name}")
        else:
            self._set_status("db", "error", self.db.last_error or "연결 실패")
            raise RuntimeError("DB 연결 실패")

        self.tokens = TokenManager(self.cfg.kiwoom, self.env, self.cfg.kiwoom.http_timeout_sec)
        self.rest = KiwoomRest(self.cfg.kiwoom, self.env, self.tokens, on_call=self._on_api_call)
        self._set_status("kiwoom_rest", "warn", "토큰 발급 중")
        self.tokens.get_token()
        self._set_status("kiwoom_rest", "ok", f"토큰 발급됨(만료 {self.tokens.expires_at})")

        self.accounts = AccountService(self.db, self.rest)
        self.account_id, self.account_no = self.accounts.ensure_account(self.env)
        self.status.set(account_id=self.account_id,
                        account_no_masked=mask_account_no(self.account_no))
        log.info("계좌 확인: %s (account_id=%s)", mask_account_no(self.account_no), self.account_id)

        self.market = MarketService(self.db, self.rest)
        self.orders = OrderSyncService(self.db, self.rest, self.account_id)
        self.house = HousekeepingService(self.db, self.rest, self.account_id)
        self.run_id = self.db.start_run(self.env, gate.can_send_order,
                                        note=f"gate: {gate.describe()}")
        self.status.set(run_id=self.run_id)
        # Executor 는 주문 직전마다 auto_trading 플래그를 재확인한다(중지 즉시 반영).
        self.executor = Executor(self.db, self.rest, self.account_id, self.run_id,
                                 auto_trading_check=lambda: self.auto_trading_active,
                                 on_critical=self._on_critical)
        # 안전: 이전 상태를 복원하지 않고 항상 '중지'로 시작한다.
        self._auto_trading.clear()
        self._publish_auto_status()

        self._safe("보관기간 정리", lambda: self.house.run_purge(
            self._retention_days(), self.cfg.logging.path))
        self._safe("종목마스터 갱신", self._sync_master_if_needed)
        self._safe("계좌 동기화", lambda: self.accounts.sync(self.account_id))
        self._start_ws()
        self._set_status("server", "ok", "엔진 기동")
        self.db.log_event("INFO", "system", f"엔진 시작 (env={self.env}, 게이트 {gate.describe()})")

    def _start_ws(self) -> None:
        assert self.tokens
        self.ws = KiwoomWebSocket(
            self.cfg.kiwoom.ws_url(self.env),
            token_provider=lambda: self.tokens.get_token(),
            on_real=self._on_real,
            on_status=lambda s, m: self._set_status("kiwoom_ws", s, m),
        )
        self.ws.subscribe("1", [""], [TYPE_ORDER_EXEC, TYPE_BALANCE, TYPE_MARKET_TIME])
        self.ws.start()

    # -- 루프 ---------------------------------------------------------- #
    def _loop(self) -> None:
        last_heartbeat = 0.0
        last_account = 0.0
        last_orders = 0.0
        last_eval = 0.0
        while not self._stop.is_set():
            now_mono = time.monotonic()
            open_now = self.market_open()
            self._set_market_status(open_now)

            if now_mono - last_heartbeat >= HEARTBEAT_SEC:
                last_heartbeat = now_mono
                self._heartbeat()

            acct_every = ACCOUNT_SYNC_OPEN_SEC if open_now else ACCOUNT_SYNC_CLOSED_SEC
            if now_mono - last_account >= acct_every:
                last_account = now_mono
                self._safe("계좌 동기화", lambda: self.accounts.sync(self.account_id))

            ord_every = ORDER_SYNC_OPEN_SEC if open_now else ORDER_SYNC_CLOSED_SEC
            if now_mono - last_orders >= ord_every:
                last_orders = now_mono
                ok1 = self._safe("미체결 동기화", self.orders.sync_open_orders)
                ok2 = self._safe("체결 동기화", self.orders.sync_executions)
                if ok1 and ok2:
                    # S-01/R-03 복구 경로: 동기화 결과에 **증거**가 있는 건만 해제한다
                    self._safe("접수여부 불명 확인", self._review_pending_unknown)
                # R-07: 주문번호 없이 SENT 로 남은 주문을 정리(영구 차단 방지)
                self._safe("미확정 주문 정리", self._expire_unknown_orders)

            if self._reset_eval:
                self._reset_eval = False
                last_eval = 0.0
            poll = self._poll_interval()
            if not self.auto_trading_active:
                # 자동거래 중지 상태: 평가 자체를 하지 않는다(신호/주문 미생성)
                last_eval = now_mono
            elif now_mono - last_eval >= poll:
                last_eval = now_mono
                self._safe("알고리즘 평가", self.run_cycle)

            if not open_now:
                self._safe("장마감 정리", self._eod_if_needed)
                self._safe("보관기간 정리", lambda: self.house.run_purge(
                    self._retention_days(), self.cfg.logging.path))
                self._safe("종목마스터 갱신", self._sync_master_if_needed)

            self._stop.wait(TICK_SEC)

    # -- 종료 ---------------------------------------------------------- #
    def _shutdown(self) -> None:
        log.info("엔진 종료 시작")
        if self._auto_trading.is_set():
            self._safe("자동거래 중지", lambda: self.stop_auto_trading(
                by="엔진 종료", reason="엔진(조회·동기화) 정지"))
        if self.ws:
            self._safe("WS 종료", lambda: self.ws.stop(timeout=5))
        if self.run_id:
            self._safe("run 종료 기록", lambda: self.db.end_run(self.run_id, "정상 종료"))
        if self.tokens:
            self._safe("토큰 폐기", self.tokens.revoke)
        if self.rest:
            self._safe("HTTP 종료", self.rest.close)
        for comp in ("server", "kiwoom_rest", "kiwoom_ws", "market", AUTO_COMPONENT):
            try:
                self.db.set_status(comp, "unknown",
                                   AUTO_TEXT_STOPPED if comp == AUTO_COMPONENT else "서버 종료")
            except Exception:  # noqa: BLE001
                pass
        try:
            self.db.log_event("INFO", "system", "엔진 종료")
        except Exception:  # noqa: BLE001
            pass
        self.status.set(kiwoom_rest=("unknown", "종료"), kiwoom_ws=("unknown", "종료"),
                        market=("unknown", ""), running=False, auto_trading=False,
                        auto_status=("unknown", AUTO_TEXT_STOPPED))
        log.info("엔진 종료 완료")

    # ================================================================== #
    # 평가 사이클
    # ================================================================== #
    def build_context(self, force_market: bool = False, observe_only: bool = False) -> EngineContext:
        ctx = EngineContext.build(self.db, self.account_id, market=self.market, run_id=self.run_id,
                                  market_open=self.market_open(),
                                  anthropic_cfg=self.cfg.anthropic)
        if force_market:
            ctx.market_open = True
            ctx.note("force_market=True (진단용 강제 평가)")
        if observe_only:
            # S-09: CLI 진단 경로는 게이트를 **강제로 닫아** 어떤 주문도 나가지 않게 한다.
            ctx.gate = OrderGateState(order_enabled=False,
                                      trading_mode=ctx.gate.trading_mode,
                                      real_trading_confirm=False)
            ctx.note("observe_only=True (관찰 전용 - 주문 전송 불가)")
        # R-03: 평가 주기가 짧아도 동일 종목 재주문 쿨다운은 기본값(5분) 아래로 내려가지 않는다.
        ctx.cooldown_sec = max(DEFAULT_COOLDOWN_SEC, self._poll_interval() * 3)
        return ctx

    def market_open(self) -> bool:
        """장 운영 여부. WS `0s` 수신값을 우선 사용하고(B5), 실시간 미연결이면
        시간대 판단 결과와 AND 하여 **보수적으로** 판단한다."""
        by_clock = is_market_open()
        if self._market_code is not None:
            # 215: 0=장시작전 3=장시작 2/4=장마감
            ws_open = self._market_code == "3"
            return ws_open and by_clock
        if self.ws is not None and not self.ws.connected.is_set():
            return False          # 실시간 미연결 → 공휴일 판단 불가, 보수적으로 장외
        return by_clock

    def run_cycle(self, force_market: bool = False, all_algos: bool = False,
                  ignore_auto: bool = False, observe_only: bool = False) -> dict:
        """평가 1회. 활성 알고리즘을 DB 에서 다시 읽어 신호 → 필터 → risk_guard → Executor.

        자동거래가 중지 상태이면 평가 자체를 하지 않는다(`ignore_auto=True` 인 CLI 진단 제외).
        """
        if not ignore_auto and not self.auto_trading_active:
            return {"ctx": None, "signals": [], "results": [], "algos": [], "sent": 0,
                    "skipped": "자동거래 중지"}
        ctx = self.build_context(force_market=force_market, observe_only=observe_only)
        self.status.set(mode=ctx.gate.trading_mode.upper(),
                        order_allowed=ctx.gate.can_send_order,
                        gate_text=ctx.gate.describe())

        metas = self.db.load_algorithms()
        algos = registry.build_all(metas, enabled_only=not all_algos,
                                   on_error=self._on_algo_build_error)
        entry_like = [a for a in algos if a.role in ("entry", "risk")]
        # claude_advisor 는 role='filter' 이지만 일반 필터 단계가 아니라
        # risk_guard 뒤(= Executor 직전)에서만 동작한다(곧 주문될 매수 신호에만 호출).
        filters = [a for a in algos if a.role == "filter" and a.code != CLAUDE_CODE]
        guard = next((a for a in algos if a.code == "risk_guard"), None)
        advisor = next((a for a in algos if a.code == CLAUDE_CODE), None)

        # ★ R-01 (fail-closed): risk_guard 가 없으면 전역 한도 검사가 통째로 사라진다.
        #    예전에는 이 경우 신호가 Executor 로 직행했다(fail-open). 이제는 사이클을 중단한다.
        if guard is None:
            ctx.halt(NO_GUARD_REASON)
            self._report_no_guard(ctx)
            with self.status.lock:
                self.status.cycles += 1
                self.status.last_cycle_at = ctx.now.strftime("%Y-%m-%d %H:%M:%S")
            self._notify()
            return {"ctx": ctx, "signals": [], "results": [],
                    "algos": [a.code for a in algos], "sent": 0, "skipped": NO_GUARD_REASON}

        signals: list[Signal] = []
        for algo in entry_like:
            try:
                got = algo.evaluate(ctx) or []
            except Exception:  # noqa: BLE001 - 알고리즘 하나가 사이클을 죽이지 않게
                log.exception("알고리즘 평가 실패: %s", algo.code)
                continue
            if got:
                log.info("%s: 신호 %d건", algo.code, len(got))
            signals.extend(got)

        for filt in filters:
            try:
                signals = filt.filter_signals(ctx, signals) or []
            except Exception:  # noqa: BLE001
                log.exception("필터 실패: %s", filt.code)

        results = []
        sent = 0
        for sig in signals:
            # guard 는 위에서 존재를 보장한다(없으면 사이클이 이미 중단됐다 - R-01)
            try:
                ok, reason = guard.check(ctx, sig)
            except Exception:  # noqa: BLE001 - 검증 실패 시 보수적으로 차단
                log.exception("risk_guard 검증 오류")
                ok, reason = False, "risk_guard 오류"
            if not ok:
                log.info("risk_guard 차단: %s | %s", sig, reason)
                try:
                    self.db.insert_signal(ctx.run_id, "risk_guard", sig.stk_cd, sig.stk_nm,
                                          "BLOCK", sig.score, f"{sig.algo_code} 차단 - {reason}")
                except Exception:  # noqa: BLE001
                    pass
                continue

            # ★ Claude 검토 (매수 신호만. 매도·손절·청산은 호출조차 하지 않는다) ★
            if advisor is not None and advisor.needs_review(sig):
                passed, detail = advisor.review(ctx, sig)
                if not passed:
                    log.info("claude_advisor 차단: %s | %s", sig, detail)
                    try:
                        self.db.insert_signal(ctx.run_id, CLAUDE_CODE, sig.stk_cd, sig.stk_nm,
                                              "BLOCK", sig.score, detail)
                    except Exception:  # noqa: BLE001
                        pass
                    continue

            res = self.executor.submit(ctx, sig)
            results.append(res)
            if res.sent:
                sent += 1

        with self.status.lock:
            self.status.cycles += 1
            self.status.signals += len(signals)
            self.status.orders_sent += sent
            self.status.last_cycle_at = ctx.now.strftime("%Y-%m-%d %H:%M:%S")
        self._notify()
        return {"ctx": ctx, "signals": signals, "results": results,
                "algos": [a.code for a in algos], "sent": sent}

    # ================================================================== #
    def _heartbeat(self) -> None:
        """B7: 10초마다 6개 컴포넌트를 모두 다시 기록해 웹 관제의 지연 경고를 막는다."""
        ok = self.db.ping()
        self._set_status("db", "ok" if ok else "error",
                         "" if ok else (self.db.last_error or "연결 실패"))
        self._set_status("server", "ok", f"heartbeat {now_kst():%H:%M:%S}")
        if self.rest:
            if self.rest.last_error:
                self._set_status("kiwoom_rest", "warn", self.rest.last_error)
            else:
                self._set_status("kiwoom_rest", "ok", "정상")
        if self.ws is not None:
            connected = self.ws.connected.is_set()
            self._set_status("kiwoom_ws", "ok" if connected else "warn",
                             self.ws.last_message or ("실시간 연결됨" if connected else "재연결 중"))
        open_now = self.market_open()
        self._set_status("market", "ok" if open_now else "warn", "장중" if open_now else "장외")
        self._publish_auto_status()
        settings = self.db.get_settings()
        gate = OrderGateState.from_settings(settings)
        self.status.set(mode=gate.trading_mode.upper(), order_allowed=gate.can_send_order,
                        gate_text=gate.describe())

    def _set_market_status(self, open_now: bool) -> None:
        cur = self.status.snapshot()["market"]
        want = ("ok", "장중") if open_now else ("warn", "장외")
        if cur != want:
            self._set_status("market", want[0], want[1])

    def _poll_interval(self) -> int:
        try:
            return max(5, int(self.db.get_setting("poll_interval_sec", "30") or 30))
        except (TypeError, ValueError):
            return 30

    def _retention_days(self) -> int:
        try:
            return max(1, int(self.db.get_setting("log_retention_days", "7") or 7))
        except (TypeError, ValueError):
            return 7

    def _sync_master_if_needed(self) -> None:
        today = now_kst().date()
        if self._master_synced_date == today:
            return
        if self.db.stock_master_updated_today(today) and self.db.stock_master_count() > 0:
            self._master_synced_date = today
            return
        self.market.sync_stock_master()
        self._master_synced_date = today

    def _eod_if_needed(self) -> None:
        now = now_kst()
        if now.weekday() >= 5 or now.hour < 16:
            return
        self.house.run_end_of_day()

    def _expire_unknown_orders(self) -> None:
        """주문번호를 못 받은 채 SENT 로 남은 주문을 FAILED 로 정리한다 (R-07)."""
        if not self.account_id:
            return
        n = self.db.expire_unknown_sent_orders(self.account_id, UNKNOWN_ORDER_EXPIRE_MIN)
        if n:
            log.warning("주문번호 미확정 주문 %d건을 FAILED 로 정리했습니다"
                        "(%d분 경과)", n, UNKNOWN_ORDER_EXPIRE_MIN)
            try:
                self.db.log_event("WARN", "order",
                                  f"주문번호 미확정 주문 {n}건 정리 (UNKNOWN 확정 불가)")
            except Exception:  # noqa: BLE001
                pass

    def _review_pending_unknown(self) -> None:
        """동기화 후 '접수여부 불명' 주문을 **증거 기반**으로 확정한다 (S-01 복구 / R-03).

        동기화가 예외 없이 끝났다는 사실만으로는 해제하지 않는다.
        """
        if not (self.executor and self.executor.pending_unknown):
            return
        # 조회에 실패하면 None 을 넘겨 '증거 없음'으로 처리한다(잘못된 해제 방지)
        try:
            holdings = {h["stk_cd"]: h for h in self.db.get_holdings(self.account_id)}
        except Exception:  # noqa: BLE001
            log.warning("접수여부 확인용 보유종목 조회 실패", exc_info=True)
            holdings = None
        try:
            balance = self.db.latest_balance(self.account_id) or {}
        except Exception:  # noqa: BLE001
            log.debug("접수여부 확인용 잔고 조회 실패", exc_info=True)
            balance = {}
        cash = None
        for key in ("ord_alow_amt", "entr", "d2_entra"):
            val = balance.get(key)
            if val is not None:
                try:
                    cash = int(val)
                except (TypeError, ValueError):
                    cash = None
                break
        result = self.executor.resolve_pending_unknown(holdings, cash, now_kst())
        if result["pending"]:
            log.warning("접수여부 불명 유지(증거 없음): %s", ", ".join(result["pending"]))
        if result["critical"]:
            # 자동거래 자동 정지 + 경보는 Executor 가 on_critical 로 이미 요청했다
            self.status.set(last_error=f"접수여부 불명 미확정: {', '.join(result['critical'])}"[:200])

    def _on_algo_build_error(self, code: str, detail: str, critical: bool) -> None:
        """알고리즘이 파라미터 오류 등으로 비활성화될 때 **표면화**한다 (R-01).

        예전에는 조용히 None 이 되어 risk_guard 가 사라져도 아무 표시가 없었다.
        """
        message = f"알고리즘 비활성화: {code} - {detail}"
        if critical:
            message = "[안전장치 누락] " + message
            log.critical("%s", message)
            self._set_status("server", "error", message[:200])
        else:
            log.error("%s", message)
        try:
            self.db.log_event("ERROR", "algo", message)
        except Exception:  # noqa: BLE001
            pass
        self.status.set(last_error=message[:200])

    def _report_no_guard(self, ctx: EngineContext) -> None:
        """risk_guard 없이 돌아간 사이클을 경보로 남긴다 (R-01)."""
        log.critical("%s - 이번 사이클의 모든 신호/주문을 중단합니다", NO_GUARD_REASON)
        try:
            self.db.log_event("ERROR", "algo",
                              f"{NO_GUARD_REASON} (알고리즘 파라미터·선택 상태를 확인하세요)")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.db.set_status(AUTO_COMPONENT, "error", NO_GUARD_REASON)
        except Exception:  # noqa: BLE001
            pass
        self.status.set(auto_status=("error", NO_GUARD_REASON), last_error=NO_GUARD_REASON)
        self._notify()
        ctx.note(NO_GUARD_REASON)

    def _on_critical(self, reason: str) -> None:
        """Executor 가 치명적 불일치를 감지하면 자동거래를 즉시 중지한다(S-02)."""
        log.critical("치명적 상황 감지 - 자동거래 중지: %s", reason)
        try:
            self.db.log_event("ERROR", "order", f"자동거래 자동 중지: {reason}")
        except Exception:  # noqa: BLE001
            pass
        self.stop_auto_trading(by="시스템(자동 정지)", reason=reason)
        self.status.set(last_error=reason[:200])

    def _on_api_call(self, api_id, status, rc, msg, elapsed_ms) -> None:
        try:
            self.db.log_api_call(api_id, status, rc, msg, elapsed_ms)
        except Exception:  # noqa: BLE001
            pass

    def _on_real(self, rtype: str, item: str, values: dict) -> None:
        if rtype == TYPE_ORDER_EXEC:
            self.orders.on_order_exec(values)
        elif rtype == TYPE_BALANCE:
            self.orders.on_balance(values)
        elif rtype == TYPE_MARKET_TIME:
            code = str(values.get("215", "")).strip()
            self._market_code = code or None
            text = {"0": "장시작전", "3": "장시작", "2": "장마감", "4": "장마감"}.get(code, f"구분 {code}")
            self._set_status("market", "ok" if code == "3" else "warn", text)
            log.info("장운영 상태: %s (215=%s)", text, code)

    def _safe(self, what: str, fn) -> bool:
        """서비스 예외 격리."""
        try:
            fn()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("%s 실패: %s: %s", what, type(exc).__name__, exc)
            self.status.set(last_error=f"{what}: {type(exc).__name__}"[:200])
            self._notify()
            return False
