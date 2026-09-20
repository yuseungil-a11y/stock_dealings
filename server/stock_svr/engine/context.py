"""평가 1회분의 상태를 모아 알고리즘에 넘기는 컨텍스트."""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..db import Database
from ..util import is_market_open, now_kst

log = logging.getLogger(__name__)


VALID_MODES = ("mock", "real")

# 잔고 스냅샷이 이보다 오래되면 매수를 차단한다 (S-07: 주문가능금액·총자산 확인 불가)
BALANCE_STALE_SEC = 600

# ---------------------------------------------------------------------- #
# 거래불가(상장폐지·정리매매·거래정지) 종목 제외 규칙 (R-06)
# ---------------------------------------------------------------------- #
# 키움은 상장폐지 종목의 수익률(prft_rt)을 0 으로 내려주므로 손절 판정이 '우연히' 비켜간다.
# 우연에 기대지 않도록 아래 조건에 걸리면 손절·물타기·청산·신규진입 신호를 모두 만들지 않는다.
# (웹 화면도 같은 기준을 쓴다: web/lib/repo.php 의 HOLDING_DELISTED_PREFIX)
DELISTED_PREFIX = "(폐)"
UNTRADABLE_STATE_WORDS = ("상장폐지", "정리매매", "거래정지")

# '거래불가 종목 제외' 정보 로그를 종목별 하루 1회로 제한하기 위한 표시
_UNTRADABLE_LOGGED: set[tuple[str, _dt.date]] = set()


def reset_untradable_log_state() -> None:
    """하루 1회 로그 표시 초기화(테스트용)."""
    _UNTRADABLE_LOGGED.clear()


def gate_widened(before: "OrderGateState", after: "OrderGateState") -> bool:
    """게이트가 `before` 보다 더 열렸는지 (R-11).

    자동거래 시작 확인창에 보여준 게이트와 실제 시작 시점의 게이트를 비교하는 데 쓴다.
    """
    if after.can_send_order and not before.can_send_order:
        return True
    if (after.can_send_order and before.can_send_order
            and after.trading_mode == "real" and before.trading_mode != "real"):
        return True
    return False


@dataclass
class OrderGateState:
    """DEV_SPEC 2-3 주문 게이트 상태. 판단 로직은 여기 한 곳에만 있다.

    `trading_mode` 는 **정확히 소문자 'mock'/'real'** 만 인정한다(S-02c).
    그 외 값(대문자·오타·손상)은 오설정으로 보고 게이트를 닫는다.
    """

    order_enabled: bool = False
    trading_mode: str = "real"
    real_trading_confirm: bool = False

    @classmethod
    def from_settings(cls, settings: dict[str, str]) -> "OrderGateState":
        return cls(
            order_enabled=str(settings.get("order_enabled", "0")).strip() == "1",
            trading_mode=str(settings.get("trading_mode", "real")).strip(),
            real_trading_confirm=str(settings.get("real_trading_confirm", "0")).strip() == "1",
        )

    @property
    def valid_mode(self) -> bool:
        return self.trading_mode in VALID_MODES

    @property
    def can_send_order(self) -> bool:
        """can_send_order = order_enabled AND (mode=='mock' OR real_trading_confirm)
        (거래환경 값이 'mock'/'real' 이 아니면 무조건 닫힘)"""
        if not self.valid_mode:
            return False
        if not self.order_enabled:
            return False
        if self.trading_mode == "mock":
            return True
        return self.real_trading_confirm

    def block_reason(self) -> str:
        if not self.valid_mode:
            return f"trading_mode 값이 올바르지 않음({self.trading_mode!r}) - 주문 차단"
        if not self.order_enabled:
            return "order_enabled=0 (주문 전송 비활성)"
        if self.trading_mode != "mock" and not self.real_trading_confirm:
            return "trading_mode=real 인데 real_trading_confirm=0 (실전 이중확인 미완료)"
        return ""

    def describe(self) -> str:
        return (f"mode={self.trading_mode} order_enabled={int(self.order_enabled)} "
                f"real_confirm={int(self.real_trading_confirm)} -> "
                f"{'ON' if self.can_send_order else 'OFF'}")


@dataclass
class EngineContext:
    """알고리즘이 읽는 읽기 전용 상태 묶음."""

    db: Database
    account_id: int
    settings: dict[str, str] = field(default_factory=dict)
    gate: OrderGateState = field(default_factory=OrderGateState)
    market: Any = None                       # services.sync_market.MarketService
    anthropic_cfg: Any = None                # config.AnthropicConfig (claude_advisor 전용)
    run_id: int | None = None
    now: _dt.datetime = field(default_factory=now_kst)
    holdings: dict[str, dict] = field(default_factory=dict)
    position_states: dict[str, dict] = field(default_factory=dict)
    balance: dict[str, Any] = field(default_factory=dict)
    market_open: bool = False
    cooldown_sec: int = 300
    notes: list[str] = field(default_factory=list)
    # 이번 사이클에서 Executor 가 승인한 매수 금액 누적 (S-10-②: 총한도 중복 초과 방지)
    cycle_invested: int = 0
    cycle_invested_by_stock: dict[str, int] = field(default_factory=dict)
    # 비어 있지 않으면 이번 사이클의 **매수 주문을 차단**한다 (S-06/S-08 fail-closed).
    # R-05: 손절·청산 SELL 은 이 플래그로 막지 않는다(포지션을 빠져나갈 길을 남긴다).
    risk_halt: str = ""
    # 거래불가 판정 캐시 {stk_cd: 사유(""=거래가능)}
    _untradable: dict[str, str] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ #
    @classmethod
    def build(cls, db: Database, account_id: int, market=None, run_id: int | None = None,
              now: _dt.datetime | None = None, market_open: bool | None = None,
              anthropic_cfg=None) -> "EngineContext":
        settings = db.get_settings()
        ts = now or now_kst()
        balance = db.latest_balance(account_id) or {}
        return cls(
            db=db,
            account_id=account_id,
            settings=settings,
            gate=OrderGateState.from_settings(settings),
            market=market,
            anthropic_cfg=anthropic_cfg,
            run_id=run_id,
            now=ts,
            holdings={h["stk_cd"]: h for h in db.get_holdings(account_id)},
            position_states=db.get_position_states(account_id),
            balance=dict(balance),
            market_open=is_market_open(ts) if market_open is None else bool(market_open),
        )

    # -- fail-closed 헬퍼 ---------------------------------------------- #
    def halt(self, reason: str) -> None:
        """이번 사이클 **매수** 차단(리스크 판단 불가 시).

        R-05: 손절·청산 SELL 은 이 플래그로 막지 않는다. 리스크 계산이 불가능한 상황일수록
        포지션을 줄이는 매도는 오히려 허용해야 하기 때문이다(게이트·env 일치·거래불가
        종목 제외 등 다른 안전 검사는 그대로 적용된다).
        """
        if not self.risk_halt:
            self.risk_halt = reason
            self.note(f"주문 차단(fail-closed): {reason}")
            log.error("주문 차단(fail-closed): %s", reason)

    # -- 거래불가 종목 (R-06) ------------------------------------------ #
    def untradable_reason(self, stk_cd: str, stk_nm: str | None = None) -> str:
        """거래불가(상장폐지·정리매매·거래정지) 사유. 거래 가능하면 빈 문자열.

        판정: 보유종목의 현재가 <= 0 / 종목명이 '(폐)' 로 시작 / stock_master.state 표기.
        """
        cached = self._untradable.get(stk_cd)
        if cached is not None:
            return cached
        reason = self._detect_untradable(stk_cd, stk_nm)
        self._untradable[stk_cd] = reason
        if reason:
            self._log_untradable_once(stk_cd, reason)
        return reason

    def _detect_untradable(self, stk_cd: str, stk_nm: str | None) -> str:
        h = self.holding(stk_cd) or {}
        name = str(stk_nm or h.get("stk_nm") or "").strip()
        if name.startswith(DELISTED_PREFIX):
            return f"거래불가 종목 제외: {stk_cd} {name} (상장폐지 표기)"
        # 현재가가 NULL 이면 '아직 미동기화'로 보고 제외하지 않는다(오탐 방지).
        # 웹 화면(web/lib/repo.php holding_is_delisted)과 같은 기준이다.
        raw_cur = h.get("cur_prc")
        if raw_cur is not None:
            try:
                cur = int(raw_cur)
            except (TypeError, ValueError):
                cur = None
            if cur is not None and cur <= 0:
                return f"거래불가 종목 제외: {stk_cd} {name or ''} (현재가 0)".replace("  ", " ")
        getter = getattr(self.db, "stock_state", None)
        if callable(getter):
            try:
                state = str(getter(stk_cd) or "")
            except Exception:  # noqa: BLE001 - 조회 실패는 판정 생략(다른 검사가 방어)
                log.debug("stock_master.state 조회 실패 %s", stk_cd, exc_info=True)
                state = ""
            for word in UNTRADABLE_STATE_WORDS:
                if word in state:
                    return f"거래불가 종목 제외: {stk_cd} {name or ''} ({state.strip()})".replace(
                        "  ", " ")
        return ""

    def _log_untradable_once(self, stk_cd: str, reason: str) -> None:
        """같은 종목은 하루 1회만 정보로 남긴다."""
        key = (stk_cd, self.now.date())
        if key in _UNTRADABLE_LOGGED:
            return
        _UNTRADABLE_LOGGED.add(key)
        log.info("%s", reason)
        self.note(reason)
        try:
            self.db.log_event("INFO", "algo", reason)
        except Exception:  # noqa: BLE001
            log.debug("거래불가 종목 기록 실패", exc_info=True)

    def balance_age_sec(self) -> float | None:
        """잔고 스냅샷이 얼마나 오래됐는지(초). 알 수 없으면 None."""
        snap = self.balance.get("snapshot_at")
        if not isinstance(snap, _dt.datetime):
            return None
        return max(0.0, (self.now - snap).total_seconds())

    def total_asset(self) -> tuple[int | None, str]:
        """총자산(추정예탁자산, 원)과 오류 사유.

        비중(%) 한도 계산의 기준값이다. 값이 없거나 0 이거나 스냅샷이 오래되면
        `(None, 사유)` 를 돌려주고 호출부가 매수를 차단한다(S-07 과 같은 fail-closed 정책).
        """
        if not self.balance:
            return None, "총자산 확인 불가 (잔고 스냅샷 없음)"
        age = self.balance_age_sec()
        if age is None:
            return None, "총자산 확인 불가 (잔고 시각 불명)"
        if age > BALANCE_STALE_SEC:
            return None, f"총자산 확인 불가 (잔고 스냅샷 {int(age)}초 경과)"
        raw = self.balance.get("prsm_dpst_aset_amt")
        try:
            asset = int(raw or 0)
        except (TypeError, ValueError):
            return None, "총자산 확인 불가 (추정예탁자산 값 오류)"
        if asset <= 0:
            return None, "총자산 확인 불가 (추정예탁자산 0)"
        return asset, ""

    # -- 편의 --------------------------------------------------------- #
    @property
    def trading_mode(self) -> str:
        return self.gate.trading_mode

    @property
    def env(self) -> str:
        return "mock" if self.gate.trading_mode == "mock" else "real"

    def holding(self, stk_cd: str) -> dict | None:
        return self.holdings.get(stk_cd)

    def position(self, stk_cd: str) -> dict:
        return self.position_states.get(stk_cd) or {}

    def cash_available(self) -> int:
        for key in ("ord_alow_amt", "entr", "d2_entra"):
            val = self.balance.get(key)
            if val is not None:
                try:
                    return int(val)
                except (TypeError, ValueError):
                    continue
        return 0

    def total_invested(self) -> int:
        """position_state 기준 알고리즘 누적 투입금 + 이번 사이클 승인분."""
        total = int(self.cycle_invested)
        for st in self.position_states.values():
            try:
                total += int(st.get("total_invested") or 0)
            except (TypeError, ValueError):
                pass
        return total

    def invested_in(self, stk_cd: str) -> int:
        st = self.position(stk_cd)
        try:
            base = int(st.get("total_invested") or 0)
        except (TypeError, ValueError):
            base = 0
        return base + int(self.cycle_invested_by_stock.get(stk_cd, 0))

    def record_cycle_invest(self, stk_cd: str, amount: int) -> None:
        """Executor 가 매수를 승인할 때 사이클 누적 투입액을 반영한다(S-10-②).

        R-12: 호출부는 risk_guard 의 한도 검사와 **같은 기준**(시장가/최유리는 슬리피지
        버퍼 ×1.1 적용)의 금액을 넘긴다. 그렇지 않으면 같은 사이클의 다음 신호가
        실제보다 작은 누적액으로 한도를 통과할 수 있다.
        """
        amt = max(0, int(amount))
        self.cycle_invested += amt
        self.cycle_invested_by_stock[stk_cd] = self.cycle_invested_by_stock.get(stk_cd, 0) + amt

    def holding_profit_rate(self, stk_cd: str) -> Decimal | None:
        h = self.holding(stk_cd)
        if not h:
            return None
        rt = h.get("prft_rt")
        if rt is None:
            pur, cur = h.get("pur_pric"), h.get("cur_prc")
            if pur and cur:
                return (Decimal(cur) - Decimal(pur)) / Decimal(pur) * 100
            return None
        return Decimal(str(rt))

    def current_price(self, stk_cd: str, fallback_quote: bool = True) -> int | None:
        h = self.holding(stk_cd)
        if h and h.get("cur_prc"):
            return int(h["cur_prc"])
        if fallback_quote and self.market is not None:
            try:
                q = self.market.quote(stk_cd)
                if q and q.get("cur_prc"):
                    return int(q["cur_prc"])
            except Exception:  # noqa: BLE001 - 시세 조회 실패는 신호 생략으로 처리
                log.debug("현재가 조회 실패 %s", stk_cd, exc_info=True)
        return None

    def note(self, text: str) -> None:
        self.notes.append(text)
