"""주문 실행기 — **주문 게이트가 존재하는 유일한 지점**.

DEV_SPEC 2-3:
    can_send_order = (order_enabled == '1') AND (trading_mode == 'mock' OR real_trading_confirm == '1')
하나라도 거짓이면 `orders` 에 `is_dry_run=1, status='SIGNAL_ONLY'` 로만 기록하고 API 를 호출하지 않는다.

여기에 더해 **자동거래(auto_trading) 활성 여부를 선행 조건**으로 주문 직전마다 재확인한다.
게이트 진리표 자체는 바뀌지 않으며, 자동거래가 중지되면 사이클 도중이라도 이후 전송이 멈춘다.

키움 주문 API(kt10000/kt10001)는 `KiwoomRest.unlock_orders()` 안에서만 호출할 수 있고,
그 컨텍스트를 여는 코드는 이 파일의 `_send_order()` 하나뿐이다.
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass
from typing import Callable

from ..algo.base import KIND_AVG_DOWN, KIND_STOP_LOSS, Signal, amount_with_buffer
from ..db import Database
from ..kiwoom.errors import KiwoomError
from .context import BALANCE_STALE_SEC, EngineContext, OrderGateState  # noqa: F401

log = logging.getLogger(__name__)

BUY_API = "kt10000"
SELL_API = "kt10001"
CANCEL_API = "kt10003"

# 동일 종목 재신호 쿨다운 기본값(초)
DEFAULT_COOLDOWN_SEC = 300

# 전송했지만 응답을 받지 못해 접수 여부를 알 수 없는 주문 표시 (S-01)
UNKNOWN_PREFIX = "UNKNOWN: 접수여부 불명 -"

# --- 접수여부 불명 주문의 해제 조건 (R-03) --------------------------- #
# 동기화가 '예외 없이 끝났다'는 사실만으로는 아무것도 확정되지 않는다.
# 종목별 증거(체결/보유수량·예수금 변화/미체결 목록에 해당 주문 등장)가 확인될 때까지 유지하고,
# 아래 시간·횟수를 넘도록 확정되지 않으면 자동거래를 정지시킨다.
PENDING_UNKNOWN_TIMEOUT_SEC = 300          # 5분
PENDING_UNKNOWN_MIN_SYNCS = 3              # 연속 동기화 3회

# --- 매도 연속 실패 봉인 (R-06) -------------------------------------- #
MAX_SELL_FAILURES = 3                      # 연속 REJECTED/FAILED 이 횟수면 종목 봉인
FAILURE_BACKOFF_SEC = 60                   # 실패 1회마다 최소 이만큼 쉬었다 재시도


@dataclass
class PendingUnknown:
    """전송했지만 접수 여부를 확인하지 못한 주문 1건 (S-01/R-03)."""

    stk_cd: str
    side: str
    qty: int
    since: _dt.datetime
    order_id: int | None = None
    syncs: int = 0                      # 증거 없이 지나간 동기화 횟수
    baseline_qty: int = 0               # 전송 직전 보유수량
    baseline_cash: int = 0              # 전송 직전 주문가능금액

    def stale_sec(self, now: _dt.datetime) -> float:
        return max(0.0, (now - self.since).total_seconds())

    def exhausted(self, now: _dt.datetime) -> bool:
        return (self.syncs >= PENDING_UNKNOWN_MIN_SYNCS
                and self.stale_sec(now) >= PENDING_UNKNOWN_TIMEOUT_SEC)


def _is_unknown_state(exc: Exception) -> bool:
    """응답을 받지 못한(=접수 여부 불명) 실패인지 판정.

    거래소가 거부(return_code != 0)한 경우는 '미접수'가 확실하므로 제외한다.
    """
    from ..kiwoom.errors import KiwoomApiError

    if isinstance(exc, KiwoomApiError):
        return False
    return True


@dataclass
class ExecResult:
    """주문 처리 결과."""

    signal: Signal
    order_id: int | None = None
    signal_id: int | None = None
    sent: bool = False
    dry_run: bool = True
    status: str = "SIGNAL_ONLY"
    ord_no: str | None = None
    blocked_reason: str = ""
    error: str | None = None
    skipped: bool = False
    auto_trading: bool = True
    unknown_state: bool = False   # 전송 후 응답 유실 - 접수 여부 불명(S-01)

    @property
    def ok(self) -> bool:
        return self.error is None and not self.skipped


class Executor:
    """신호 → (자동거래 스위치 + 주문 게이트) → 주문/신호기록."""

    def __init__(self, db: Database, rest, account_id: int, run_id: int | None = None,
                 cooldown_sec: int = DEFAULT_COOLDOWN_SEC,
                 auto_trading_check: Callable[[], bool] | None = None,
                 on_critical: Callable[[str], None] | None = None):
        self.db = db
        self.rest = rest
        self.account_id = account_id
        self.run_id = run_id
        self.cooldown_sec = cooldown_sec
        # 주문 직전마다 호출된다. None 이면 항상 허용(CLI 진단/단위테스트 기본값).
        self.auto_trading_check = auto_trading_check
        # 환경 불일치 등 치명적 상황에서 엔진 정지를 요청하는 콜백
        self.on_critical = on_critical
        # 접수 여부가 불명확한 주문 {종목코드: PendingUnknown}
        # 증거로 확정될 때까지 해당 종목의 신규 주문을 차단한다 (S-01/R-03)
        self.pending_unknown: dict[str, PendingUnknown] = {}
        # 종목별 주문 실패 {종목코드: (연속 실패 횟수, 다음 시도 가능 시각)} (R-06)
        self.failures: dict[str, tuple[int, _dt.datetime]] = {}

    def auto_trading_active(self) -> bool:
        if self.auto_trading_check is None:
            return True
        try:
            return bool(self.auto_trading_check())
        except Exception:  # noqa: BLE001 - 확인 실패 시 보수적으로 중지 취급
            log.debug("auto_trading 확인 실패", exc_info=True)
            return False

    # ------------------------------------------------------------------ #
    def submit(self, ctx: EngineContext, signal: Signal) -> ExecResult:
        """신호 1건 처리. 예외를 밖으로 던지지 않는다."""
        result = ExecResult(signal=signal)

        if signal.qty <= 0:
            result.skipped = True
            result.blocked_reason = "주문수량 0"
            self._log_signal(ctx, signal, "BLOCK", f"수량 0 ({signal.reason})")
            return result

        dup = self._duplicate_reason(ctx, signal)
        if dup:
            result.skipped = True
            result.blocked_reason = dup
            self._log_signal(ctx, signal, "BLOCK", dup)
            log.info("신호 스킵(%s): %s", dup, signal)
            return result

        # 주문 직전 재확인(순서 중요):
        #   ① 사이클 fail-closed 플래그(매수만) → ② 자동거래 스위치 → ③ 거래불가 종목 →
        #   ④ env/mode 일치 → ⑤ DB 재조회 게이트(TOCTOU) → ⑥ ctx 게이트(DEV_SPEC 2-3 진리표)
        auto_active = self.auto_trading_active()
        result.auto_trading = auto_active
        block = self._pre_send_block_reason(ctx, auto_active, signal)
        can_send = not block
        result.dry_run = not can_send
        result.status = "SENT" if can_send else "SIGNAL_ONLY"
        result.blocked_reason = block

        order_id = self.db.insert_order(
            account_id=self.account_id,
            run_id=ctx.run_id or self.run_id,
            algo_code=signal.algo_code,
            ord_no=None,
            orig_ord_no=None,
            side=signal.side,
            order_kind="NEW",
            stk_cd=signal.stk_cd,
            stk_nm=(signal.stk_nm or None),
            dmst_stex_tp=signal.exchange or "KRX",
            trde_tp=signal.trde_tp,
            ord_qty=int(signal.qty),
            ord_uv=int(signal.price) if signal.price else None,
            status=result.status,
            filled_qty=0,
            avg_fill_pric=None,
            reason=(signal.reason or "")[:255],
            return_code=None,
            return_msg=(result.blocked_reason or None),
            is_dry_run=0 if can_send else 1,
        )
        result.order_id = order_id
        result.signal_id = self._log_signal(
            ctx, signal, signal.side, signal.reason, order_id=order_id)

        if not can_send:
            log.info("[관찰모드] 신호만 기록: %s | %s", signal, result.blocked_reason)
            if signal.kind == KIND_AVG_DOWN:
                # B9: 관찰모드에서는 실제 매수가 없으므로 물타기 회차/쿨다운이 증가하지 않는다
                log.info("[관찰모드] %s 물타기 회차는 증가하지 않습니다"
                         "(실거래 시 회차·쿨다운이 적용됨)", signal.stk_cd)
            return result

        # ---- 여기서부터만 실제 주문 전송 (재시도 없음: S-01) ---- #
        try:
            ord_no, rc, rmsg = self._send_order(signal)
            result.ord_no = ord_no
            result.sent = True
            result.status = "SENT"
            self.db.update_order(order_id, ord_no=ord_no, status="SENT",
                                 return_code=rc, return_msg=(rmsg or "")[:255] or None)
            log.warning("주문 전송: %s ord_no=%s", signal, ord_no)
        except Exception as exc:  # noqa: BLE001 - 주문은 절대 재시도하지 않는다
            rc = getattr(exc, "return_code", None)
            result.error = f"{type(exc).__name__}: {exc}"
            result.status = "FAILED"
            result.unknown_state = _is_unknown_state(exc)
            msg = (f"{UNKNOWN_PREFIX} {result.error}" if result.unknown_state
                   else result.error)[:255]
            self.db.update_order(order_id, status="FAILED", return_code=rc, return_msg=msg)
            self._record_failure(ctx, signal)
            if result.unknown_state:
                # 응답 유실: 접수 여부를 알 수 없다 → 재전송 금지, **증거**로 확정할 때까지 차단
                self._mark_pending_unknown(ctx, signal, order_id)
                ctx.halt(f"{signal.stk_cd} 주문 접수여부 불명 - ka10075/ka10076 동기화로 확정 필요")
                log.critical("주문 응답 유실(접수여부 불명): %s | %s", signal, exc)
                self._raise_alarm(f"주문 접수여부 불명: {signal.stk_cd} ({exc})")
            else:
                log.error("주문 전송 실패: %s | %s", signal, exc)
            return result

        # 성공 경로: 사이클 누적 투입액 반영 + position_state 갱신
        self.failures.pop(signal.stk_cd, None)
        if signal.is_buy:
            # R-12: risk_guard 의 한도 검사와 **같은 금액**(시장가는 ×1.1)을 누적한다
            ctx.record_cycle_invest(signal.stk_cd, amount_with_buffer(signal))
        if not self._update_position_state(signal):
            ctx.halt(f"position_state 갱신 실패({signal.stk_cd}) - 한도 계산 신뢰 불가")
            self._raise_alarm(f"position_state 갱신 실패: {signal.stk_cd}")
        return result

    # ------------------------------------------------------------------ #
    def _send_order(self, signal: Signal) -> tuple[str | None, int | None, str]:
        """실제 키움 주문 API 호출. **게이트를 통과한 경로에서만 호출된다.**"""
        api_id = BUY_API if signal.is_buy else SELL_API
        body = {
            "dmst_stex_tp": signal.exchange or "KRX",
            "stk_cd": signal.stk_cd,
            "ord_qty": str(int(signal.qty)),
            "ord_uv": str(int(signal.price)) if signal.price else "",
            "trde_tp": signal.trde_tp,
            "cond_uv": "",
        }
        with self.rest.unlock_orders():
            data, _ = self.rest.call(api_id, body)
        ord_no = (data.get("ord_no") or "").strip() or None
        rc = data.get("return_code")
        try:
            rc = int(rc) if rc is not None else None
        except (TypeError, ValueError):
            rc = None
        return ord_no, rc, str(data.get("return_msg", ""))

    # ------------------------------------------------------------------ #
    def _pre_send_block_reason(self, ctx: EngineContext, auto_active: bool,
                               signal: Signal | None = None) -> str:
        """주문 전송을 막아야 할 이유를 돌려준다(빈 문자열이면 전송 허용).

        S-02/S-24: env↔mode 일치 검증 + 게이트 설정 DB 재조회(TOCTOU 차단).
        S-06/S-08: 리스크 판단 불가(`ctx.risk_halt`) 시 fail-closed.
        R-05: 단, `risk_halt` 는 **매수에만** 적용한다. 리스크 계산이 불가능한 상황에서
              손절·청산 매도까지 막으면 손실이 방치되므로 매도는 나머지 검사만 거쳐 통과한다.
        """
        is_buy = True if signal is None else signal.is_buy
        if ctx.risk_halt and is_buy:
            return f"리스크 판단 불가로 차단: {ctx.risk_halt}"
        if not auto_active:
            return "자동거래 중지됨 (주문 전송 중단)"
        # R-06: 거래불가(상장폐지·정리매매) 종목은 매수·매도 모두 전송하지 않는다
        if signal is not None:
            untradable = ctx.untradable_reason(signal.stk_cd, signal.stk_nm)
            if untradable:
                return untradable
        return self._gate_block_reason(ctx)

    def _gate_block_reason(self, ctx: EngineContext) -> str:
        """게이트 진리표 + env↔mode 일치 + 게이트 DB 재조회(TOCTOU).

        주문 경로와 **취소 경로가 같은 수준**의 검증을 받도록 한 곳에 모았다 (R-04).
        """
        if not ctx.gate.can_send_order:
            return ctx.gate.block_reason()

        # (a) 실행 환경과 거래 모드 일치 — 실전 키로 모의 주문(또는 반대) 방지
        rest_env = getattr(self.rest, "env", None)
        if rest_env is not None and rest_env != ctx.gate.trading_mode:
            reason = (f"환경 불일치: REST env={rest_env} ≠ trading_mode={ctx.gate.trading_mode}"
                      " - 주문 차단 및 엔진 정지 요청")
            log.critical("%s", reason)
            ctx.halt(reason)
            self._raise_alarm(reason)
            return reason

        # (b) 게이트 3개 설정을 DB 에서 재조회해 재확인 (평가 시점 이후 변경 대응)
        try:
            fresh = OrderGateState.from_settings(self.db.get_settings())
        except Exception as exc:  # noqa: BLE001 - 재확인 실패 시 보수적으로 차단
            reason = f"게이트 재확인 실패({type(exc).__name__}) - 주문 차단"
            log.error("%s", reason)
            ctx.halt(reason)
            return reason
        if not fresh.can_send_order:
            return f"주문 게이트가 방금 닫힘: {fresh.block_reason()}"
        if (fresh.trading_mode != ctx.gate.trading_mode
                or fresh.order_enabled != ctx.gate.order_enabled
                or fresh.real_trading_confirm != ctx.gate.real_trading_confirm):
            reason = f"게이트 설정 변경 감지({ctx.gate.describe()} → {fresh.describe()}) - 이번 주문 보류"
            log.warning("%s", reason)
            return reason
        if rest_env is not None and rest_env != fresh.trading_mode:
            return f"환경 불일치(재확인): REST env={rest_env} ≠ {fresh.trading_mode}"
        return ""

    def _raise_alarm(self, reason: str) -> None:
        """치명적 불일치 발생 시 엔진에 정지를 요청한다."""
        if self.on_critical is None:
            return
        try:
            self.on_critical(reason)
        except Exception:  # noqa: BLE001
            log.exception("치명 알람 콜백 실패")

    # ------------------------------------------------------------------ #
    # 접수여부 불명 주문 (S-01 / R-03)
    # ------------------------------------------------------------------ #
    def _mark_pending_unknown(self, ctx: EngineContext, signal: Signal,
                              order_id: int | None) -> None:
        """전송 직후 상태를 기록해 둔다. 나중에 '변화가 있었는지'를 이것과 비교한다."""
        holding = ctx.holding(signal.stk_cd) or {}
        try:
            qty = int(holding.get("rmnd_qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        self.pending_unknown[signal.stk_cd] = PendingUnknown(
            stk_cd=signal.stk_cd, side=signal.side, qty=int(signal.qty), since=ctx.now,
            order_id=order_id, baseline_qty=qty, baseline_cash=ctx.cash_available())

    def resolve_pending_unknown(self, holdings: dict[str, dict] | None, cash: int | None,
                                now: _dt.datetime) -> dict:
        """동기화 직후 호출. **종목별 증거**가 확인된 건만 해제한다 (R-03).

        증거로 인정하는 것:
          · 보유수량이 전송 시점과 달라짐 (체결됨)
          · 주문가능금액이 달라짐 (증거금이 잡혔거나 체결됨)
          · ka10075 동기화 결과 같은 종목·방향의 미체결 주문이 DB 에 있음 (접수 확정)
          · ka10076 동기화 결과 전송 시각 이후 체결이 있음

        확정하지 못한 건은 유지하고 카운트를 올린다. 5분 + 연속 3회를 넘기면
        `on_critical` 로 자동거래를 정지시킨다.

        `holdings`/`cash` 가 None 이면 '조회 실패 = 증거 없음'으로 본다(fail-closed).

        반환: {"cleared": [...], "pending": [...], "critical": [...]}
        """
        out = {"cleared": [], "pending": [], "critical": []}
        for stk_cd, item in list(self.pending_unknown.items()):
            evidence = self._evidence_for(item, holdings, now)
            if evidence:
                self.pending_unknown.pop(stk_cd, None)
                out["cleared"].append(f"{stk_cd}({evidence})")
                continue
            if cash is not None and cash != item.baseline_cash:
                self.pending_unknown.pop(stk_cd, None)
                out["cleared"].append(f"{stk_cd}(주문가능금액 변화)")
                continue
            item.syncs += 1
            if item.exhausted(now):
                out["critical"].append(stk_cd)
            else:
                out["pending"].append(stk_cd)
        if out["cleared"]:
            log.warning("접수여부 불명 해제(증거 확인): %s", ", ".join(out["cleared"]))
            try:
                self.db.log_event("WARN", "order",
                                  f"접수여부 불명 주문 확정: {', '.join(out['cleared'])}")
            except Exception:  # noqa: BLE001
                log.debug("접수여부 불명 해제 기록 실패", exc_info=True)
        if out["critical"]:
            reason = ("주문 접수여부를 확정하지 못했습니다: "
                      f"{', '.join(out['critical'])} "
                      f"({PENDING_UNKNOWN_TIMEOUT_SEC}초·동기화 {PENDING_UNKNOWN_MIN_SYNCS}회 경과)")
            log.critical("%s", reason)
            self._raise_alarm(reason)
        return out

    def _evidence_for(self, item: PendingUnknown, holdings: dict[str, dict] | None,
                      now: _dt.datetime) -> str:
        if holdings is not None:
            h = holdings.get(item.stk_cd) or {}
            try:
                qty = int(h.get("rmnd_qty") or 0)
            except (TypeError, ValueError):
                qty = 0
            if qty != item.baseline_qty:
                return "보유수량 변화"
        try:
            if self.db.has_open_order(self.account_id, item.stk_cd, item.side):
                return "미체결 목록에서 확인"
        except Exception:  # noqa: BLE001 - 조회 실패는 '증거 없음'으로 본다(fail-closed)
            log.debug("미체결 조회 실패(접수여부 확인)", exc_info=True)
        getter = getattr(self.db, "count_executions_since", None)
        if callable(getter):
            try:
                if getter(self.account_id, item.stk_cd, item.since) > 0:
                    return "체결 확인"
            except Exception:  # noqa: BLE001
                log.debug("체결 조회 실패(접수여부 확인)", exc_info=True)
        return ""

    # ------------------------------------------------------------------ #
    # 종목별 주문 실패 백오프 / 매도 봉인 (R-06)
    # ------------------------------------------------------------------ #
    def _record_failure(self, ctx: EngineContext, signal: Signal) -> None:
        """주문 실패를 종목별로 누적. 매도가 연속 실패하면 봉인한다(무한 재시도 금지)."""
        count = self.failures.get(signal.stk_cd, (0, ctx.now))[0] + 1
        until = ctx.now + _dt.timedelta(seconds=FAILURE_BACKOFF_SEC * count)
        self.failures[signal.stk_cd] = (count, until)
        if signal.side == "SELL" and count >= MAX_SELL_FAILURES:
            reason = (f"{signal.stk_cd} 매도 주문 연속 {count}회 실패 - 종목 봉인"
                      "(position_state.stopped=1)")
            log.error("%s", reason)
            try:
                self.db.upsert_position_state(self.account_id, signal.stk_cd, stopped=1)
            except Exception:  # noqa: BLE001
                log.warning("매도 봉인 기록 실패: %s", signal.stk_cd, exc_info=True)
            try:
                self.db.log_event("ERROR", "order", reason)
            except Exception:  # noqa: BLE001
                pass
            self._raise_alarm(reason)

    def _failure_backoff_reason(self, ctx: EngineContext, signal: Signal) -> str:
        entry = self.failures.get(signal.stk_cd)
        if entry is None:
            return ""
        count, until = entry
        if signal.side == "SELL" and count >= MAX_SELL_FAILURES:
            return f"매도 연속 실패 {count}회로 봉인된 종목 - 수동 확인 필요"
        if isinstance(until, _dt.datetime) and ctx.now < until:
            return (f"직전 주문 실패({count}회) 백오프 중 "
                    f"- {int((until - ctx.now).total_seconds())}초 후 재시도")
        return ""

    # ------------------------------------------------------------------ #
    def submit_cancel(self, ctx: EngineContext, ord_no: str, stk_cd: str, qty: int,
                      exchange: str = "KRX", reason: str = "긴급 미체결 취소") -> ExecResult:
        """미체결 주문 취소(kt10003). 자동거래 중지 상태에서도 쓸 수 있는 **긴급 수단**이라
        auto_trading 선행조건은 요구하지 않지만, R-04 이후로는 게이트 검증을 주문 경로와
        **동일한 수준**(env↔mode 일치 + 게이트 3키 DB 재조회)으로 매 취소 직전에 수행한다."""
        sig = Signal(algo_code="manual_cancel", stk_cd=stk_cd, side="SELL", qty=int(qty),
                     trde_tp="0", reason=reason, exchange=exchange, kind="cancel")
        result = ExecResult(signal=sig)
        block = self._gate_block_reason(ctx)
        can_send = not block
        result.dry_run = not can_send
        result.status = "SENT" if can_send else "SIGNAL_ONLY"
        result.blocked_reason = block

        order_id = self.db.insert_order(
            account_id=self.account_id, run_id=ctx.run_id or self.run_id,
            algo_code="manual_cancel", ord_no=None, orig_ord_no=ord_no,
            side="SELL", order_kind="CANCEL", stk_cd=stk_cd, stk_nm=None,
            dmst_stex_tp=exchange or "KRX", trde_tp="0", ord_qty=int(qty),
            ord_uv=None, status=result.status, filled_qty=0, avg_fill_pric=None,
            reason=reason[:255], return_code=None,
            return_msg=(result.blocked_reason or None), is_dry_run=0 if can_send else 1,
        )
        result.order_id = order_id
        if not can_send:
            log.info("[관찰모드] 취소 신호만 기록: ord_no=%s | %s", ord_no, result.blocked_reason)
            return result
        try:
            body = {"dmst_stex_tp": exchange or "KRX", "orig_ord_no": ord_no,
                    "stk_cd": stk_cd, "cncl_qty": str(int(qty))}
            with self.rest.unlock_orders():
                data, _ = self.rest.call(CANCEL_API, body, no_retry=True)
            new_no = (data.get("ord_no") or "").strip() or None
            result.ord_no = new_no
            result.sent = True
            self.db.update_order(order_id, ord_no=new_no, status="SENT")
            log.warning("미체결 취소 전송: orig=%s %s %s주 → ord_no=%s", ord_no, stk_cd, qty, new_no)
        except Exception as exc:  # noqa: BLE001 - 취소도 재시도하지 않는다
            result.error = f"{type(exc).__name__}: {exc}"
            result.status = "FAILED"
            self.db.update_order(order_id, status="FAILED", return_msg=result.error[:255])
            log.error("미체결 취소 실패: orig=%s | %s", ord_no, exc)
        return result

    def _update_position_state(self, signal: Signal) -> bool:
        """전송 성공 후 종목 상태 갱신. 실패하면 False (호출부가 이후 주문을 차단)."""
        try:
            if signal.is_buy:
                self.db.bump_position_invest(
                    self.account_id, signal.stk_cd, signal.est_amount,
                    signal.algo_code, signal.price, signal.kind == KIND_AVG_DOWN)
            else:
                self.db.upsert_position_state(
                    self.account_id, signal.stk_cd,
                    stopped=1 if signal.kind == KIND_STOP_LOSS else 0)
            return True
        except Exception:  # noqa: BLE001
            log.error("position_state 갱신 실패 (%s) - 이후 주문을 차단합니다",
                      signal.stk_cd, exc_info=True)
            return False

    # ------------------------------------------------------------------ #
    def _duplicate_reason(self, ctx: EngineContext, signal: Signal) -> str:
        """중복 주문/쿨다운/접수불명/잔고신선도 검사. **조회 실패는 fail-closed**(S-06/S-07)."""
        pending = self.pending_unknown.get(signal.stk_cd)
        if pending is not None:
            return (f"직전 주문의 접수 여부 불명({int(pending.stale_sec(ctx.now))}초 경과) "
                    "- 증거 확인 전까지 신규 주문 차단")
        fail_reason = self._failure_backoff_reason(ctx, signal)
        if fail_reason:
            return fail_reason
        try:
            # R-07: 미체결 중복 검사는 **같은 방향**만 본다.
            # (매수 미체결이 손절 매도를 막지 않게 한다)
            if self.db.has_open_order(self.account_id, signal.stk_cd, signal.side):
                return f"동일 종목 {signal.side} 미체결 주문 존재"
            last = self.db.last_order_at(self.account_id, signal.stk_cd, signal.side)
            if last:
                cooldown = ctx.cooldown_sec or self.cooldown_sec
                if isinstance(last, _dt.datetime) and (ctx.now - last).total_seconds() < cooldown:
                    return f"쿨다운({cooldown}s) 내 동일 신호"
        except Exception as exc:  # noqa: BLE001 - 판단 불가 → 차단(fail-closed)
            log.error("중복/쿨다운 검사 실패 - 주문 차단: %s", exc, exc_info=True)
            ctx.halt(f"중복 주문 검사 실패({type(exc).__name__})")
            return f"중복 검사 실패로 차단({type(exc).__name__})"

        if signal.is_buy:
            stale = self._stale_balance_reason(ctx)
            if stale:
                return stale
        return ""

    def _stale_balance_reason(self, ctx: EngineContext) -> str:
        """예수금이 0이거나 잔고 스냅샷이 오래되면 매수 차단 (S-07)."""
        if not ctx.balance:
            return "주문가능금액 확인 불가 (잔고 스냅샷 없음)"
        age = ctx.balance_age_sec()
        if age is None:
            return "주문가능금액 확인 불가 (잔고 시각 불명)"
        if age > BALANCE_STALE_SEC:
            return f"주문가능금액 확인 불가 (잔고 스냅샷 {int(age)}초 경과)"
        if ctx.cash_available() <= 0:
            return "주문가능금액 확인 불가 (주문가능금액 0)"
        return ""

    def _log_signal(self, ctx: EngineContext, signal: Signal, signal_type: str,
                    detail: str, order_id: int | None = None) -> int | None:
        try:
            return self.db.insert_signal(
                ctx.run_id or self.run_id, signal.algo_code, signal.stk_cd, signal.stk_nm,
                signal_type if signal_type in ("BUY", "SELL", "HOLD", "BLOCK") else "HOLD",
                signal.score, detail, order_id)
        except Exception:  # noqa: BLE001
            log.debug("signal_log 기록 실패", exc_info=True)
            return None
