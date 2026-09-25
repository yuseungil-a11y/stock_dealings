"""take_profit — ATR 트레일링 + 단계별 부분매도 익절.

* 평단 대비 `partial_take_pct` 이상 오르면 1차 부분매도(`partial_sell_ratio`%).
* 그 이후(또는 `trail_activate_mode=profit_pct` 이면 지정 수익률 도달 시부터) Wilder ATR
  기반 트레일링 스탑(관측 최고가 - ATR x `atr_multiplier`)이 현재가를 하회하면 잔량 전량 매도.
* **손절과 동시조건 시 손절이 항상 우선한다**: `rt <= risk_guard.stop_loss_pct` 이면 이
  알고리즘은 해당 종목에 대해 어떤 신호도 만들지 않고 risk_guard 에 맡긴다(`continue`).
  DB `algorithm_selection.priority` 는 사용자가 언제든 바꿀 수 있어 "손절/익절 중 누가
  먼저 평가되는가"의 신뢰할 수 있는 근거가 아니기 때문에, 순서에 기대지 않고 이 알고리즘
  스스로 손절선을 확인한다.
* `position_state` 는 매 사이클 `ON UPDATE CURRENT_TIMESTAMP` 로 갱신되므로(물타기
  cooldown·재진입 금지 초기화가 이를 기준으로 판단) 여기서 매 사이클 쓰는 peak_price 를
  그 테이블에 얹으면 안 된다 - 별도 `position_exit_state` 테이블을 쓴다.

주문 유형은 항상 시장가(trde_tp='3') 로 고정한다(파라미터화하지 않음) - 대기 중인
지정가 매도가 risk_guard 손절 매도를 동일방향 중복주문 검사(R-07)로 막을 수 있어서다.
"""
from __future__ import annotations

import datetime as _dt
import logging
from decimal import ROUND_FLOOR, Decimal, InvalidOperation

from .base import KIND_TAKE_PROFIT, Algorithm, Signal
from .registry import register
from .risk_guard import stop_loss_pct as guard_stop_loss_pct

log = logging.getLogger(__name__)

# 한 사이클에서 새로 계산(=일봉 조회)할 ATR 개수 상한. 보유 종목이 많아도 한 사이클이
# 모든 종목의 일봉을 한 번에 받으려다 느려지지 않게 한다. 한도에 걸린 종목은 이전 값을
# 그대로 쓰거나(값이 있으면) 트레일링 판단을 이번 사이클엔 건너뛴다(값이 없으면).
MAX_ATR_REFRESH_PER_CYCLE = 10

# 평단(진입 시점 pur_pric) 급변 판정 - 무상증자/액면분할/권리조정 등 가격 불연속 방어
ENTRY_PRICE_DISCONTINUITY_RATIO = Decimal("0.3")


# ---------------------------------------------------------------------- #
# Wilder ATR - 순수함수(단위테스트 용이)
# ---------------------------------------------------------------------- #
def true_range(high, low, prev_close) -> Decimal:
    """TR = max(고-저, |고-전일종가|, |저-전일종가|)."""
    high_d, low_d, prev_d = Decimal(high), Decimal(low), Decimal(prev_close)
    return max(high_d - low_d, abs(high_d - prev_d), abs(low_d - prev_d))


def wilder_atr(bars: list[dict], period: int) -> Decimal | None:
    """Wilder 평활 ATR.

    `bars` 는 과거→최근 순으로, 각 원소에 `high_pric`/`low_pric`/`cur_prc`(종가) 가 있어야
    한다. 첫 TR 계산에 전일 종가가 필요하므로 최소 `period + 1` 개가 필요하다.
    """
    if period <= 0 or len(bars) < period + 1:
        return None
    trs: list[Decimal] = []
    for i in range(1, len(bars)):
        high, low = bars[i].get("high_pric"), bars[i].get("low_pric")
        prev_close = bars[i - 1].get("cur_prc")
        if high is None or low is None or prev_close is None:
            return None
        trs.append(true_range(high, low, prev_close))
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / Decimal(period)
    for tr in trs[period:]:
        atr = (atr * Decimal(period - 1) + tr) / Decimal(period)
    return atr


@register
class TakeProfit(Algorithm):
    code = "take_profit"
    role = "risk"
    name = "익절(ATR 트레일링+부분매도)"

    # ------------------------------------------------------------------ #
    @staticmethod
    def validate_params(params) -> list[str]:
        errors: list[str] = []
        atr_period = params.int("atr_period", 22)
        min_bars = params.int("min_atr_bars", 23)
        if min_bars <= atr_period:
            errors.append(
                f"min_atr_bars({min_bars})는 atr_period({atr_period})보다 커야 합니다.")
        mode = params.str("trail_activate_mode", "after_partial")
        partial_pct = params.dec("partial_take_pct", 12)
        if mode == "after_partial" and partial_pct == 0:
            errors.append(
                "trail_activate_mode=after_partial 인데 partial_take_pct=0 이면 "
                "트레일링이 절대 활성화되지 않습니다(부분익절이 없으면 stage 가 오르지 않음).")
        return errors

    # ------------------------------------------------------------------ #
    def evaluate(self, ctx) -> list[Signal]:
        signals: list[Signal] = []
        if not ctx.market_open or ctx.market is None:
            return signals

        scope = self.params.str("scope", "engine") or "engine"
        partial_take_pct = self.params.dec("partial_take_pct", 12)
        partial_sell_ratio = self.params.dec("partial_sell_ratio", 40)
        atr_period = self.params.int("atr_period", 22)
        atr_multiplier = self.params.dec("atr_multiplier", 3)
        min_atr_bars = self.params.int("min_atr_bars", 23)
        trail_mode = self.params.str("trail_activate_mode", "after_partial")
        trail_activate_pct = self.params.dec("trail_activate_pct", 12)
        trail_floor_breakeven = self.params.bool("trail_floor_breakeven", True)

        stop_pct = guard_stop_loss_pct(ctx)
        today = ctx.now.date()
        atr_refreshed = 0

        for stk_cd, h in ctx.holdings.items():
            try:
                sig, used_refresh = self._evaluate_one(
                    ctx, stk_cd, h, stop_pct=stop_pct, today=today,
                    scope=scope, partial_take_pct=partial_take_pct,
                    partial_sell_ratio=partial_sell_ratio, atr_period=atr_period,
                    atr_multiplier=atr_multiplier, min_atr_bars=min_atr_bars,
                    trail_mode=trail_mode, trail_activate_pct=trail_activate_pct,
                    trail_floor_breakeven=trail_floor_breakeven,
                    atr_budget_left=max(0, MAX_ATR_REFRESH_PER_CYCLE - atr_refreshed))
            except Exception:  # noqa: BLE001 - 종목 하나의 오류가 다른 종목/알고리즘을 막지 않게
                log.exception("%s: 익절 평가 중 오류", stk_cd)
                continue
            if used_refresh:
                atr_refreshed += 1
            if sig is not None:
                signals.append(sig)
        return signals

    # ------------------------------------------------------------------ #
    def _evaluate_one(self, ctx, stk_cd: str, h: dict, *, stop_pct: Decimal,
                      today: _dt.date, scope: str, partial_take_pct: Decimal,
                      partial_sell_ratio: Decimal, atr_period: int, atr_multiplier: Decimal,
                      min_atr_bars: int, trail_mode: str, trail_activate_pct: Decimal,
                      trail_floor_breakeven: bool,
                      atr_budget_left: int) -> tuple[Signal | None, bool]:
        """종목 1개 평가. (신호 또는 None, 이번에 ATR 을 새로 계산했는지)."""
        qty = int(h.get("rmnd_qty") or 0)
        if qty <= 0:
            return None, False
        # R-06: 상장폐지·정리매매·거래정지 종목은 대상에서 뺀다
        if ctx.untradable_reason(stk_cd, h.get("stk_nm")):
            return None, False
        # scope=engine: 이 시스템의 진입 알고리즘이 산 종목만 관리(수동매수 종목 제외)
        if scope == "engine" and not ctx.position(stk_cd).get("entry_algo"):
            return None, False

        rt = ctx.holding_profit_rate(stk_cd)
        # 손절선에도 동시에 도달했으면 risk_guard 가 전담한다(동시조건시 손절 우선 - 손절만
        # position_state.stopped=1 로 당일 재진입금지를 세팅하므로, 익절이 먼저 나가버리면
        # 그 안전장치가 빠진다).
        if rt is not None and Decimal(rt) <= stop_pct:
            return None, False

        cur = int(h.get("cur_prc") or 0)
        if cur <= 0:
            cur = ctx.current_price(stk_cd) or 0
        if cur <= 0:
            return None, False
        pur = int(h.get("pur_pric") or 0)

        try:
            st = ctx.db.get_position_exit_state(ctx.account_id, stk_cd) or {}
        except Exception:  # noqa: BLE001 - 조회 실패는 빈 상태로 취급(보수적으로 처음부터 추적)
            log.debug("position_exit_state 조회 실패 %s", stk_cd, exc_info=True)
            st = {}

        stage = int(st.get("tp_stage") or 0)
        entry_pur = st.get("entry_pur_pric")
        peak_price = st.get("peak_price")
        atr_value = st.get("atr_value")
        atr_date = st.get("atr_date")
        trail_started_at = st.get("trail_started_at")
        updates: dict = {}

        if entry_pur is None:
            if pur > 0:
                entry_pur = pur
                updates["entry_pur_pric"] = pur
        else:
            # 무상증자·분할·권리조정 등으로 평단이 급변하면 추적을 리셋한다(가격 불연속 방어)
            try:
                ratio = (abs(Decimal(pur) - Decimal(entry_pur)) / Decimal(entry_pur)
                        if pur > 0 and Decimal(entry_pur) != 0 else Decimal(0))
            except (InvalidOperation, ZeroDivisionError):
                ratio = Decimal(0)
            if ratio > ENTRY_PRICE_DISCONTINUITY_RATIO:
                log.info("%s: 평단 급변 감지(%s -> %s, 비율 %.2f) - 익절 추적 리셋",
                        stk_cd, entry_pur, pur, ratio)
                peak_price = None
                atr_value = None
                atr_date = None
                entry_pur = pur
                updates.update(entry_pur_pric=pur, atr_value=None, atr_date=None)

        new_peak = cur if peak_price is None else max(int(peak_price), cur)
        if new_peak != peak_price:
            updates["peak_price"] = new_peak
        peak_price = new_peak

        used_refresh = False

        # ---- ① 부분 익절(stage 0 -> 1) ---------------------------------- #
        if stage == 0 and partial_take_pct > 0 and rt is not None and Decimal(rt) >= partial_take_pct:
            trde_able = int(h.get("trde_able_qty") or qty)
            raw_sell = (Decimal(trde_able) * partial_sell_ratio / Decimal(100))
            sell_qty = int(raw_sell.to_integral_value(rounding=ROUND_FLOOR))
            if sell_qty <= 0 or sell_qty >= trde_able:
                # 매도가능수량이 너무 적어(0 또는 보유 전량) 부분매도가 의미 없다 -
                # 매도 없이 stage 만 1로 올려 곧바로 전량 트레일링 관리로 전환한다.
                stage = 1
                updates["tp_stage"] = 1
                updates["tp_partial_qty"] = 0
                updates["tp_partial_at"] = ctx.now
                log.info("%s: 부분매도 수량 산출 불가(가능수량 %d) - 매도 없이 트레일링 단계로 전환",
                         stk_cd, trde_able)
            else:
                self._persist(ctx, stk_cd, updates)
                sig = Signal(
                    algo_code=self.code, stk_cd=stk_cd, stk_nm=h.get("stk_nm"),
                    side="SELL", qty=sell_qty, price=None, trde_tp="3",
                    kind=KIND_TAKE_PROFIT, score=float(rt),
                    reason=(f"부분 익절 (수익률 {rt}% >= {partial_take_pct}%) "
                            f"매매가능 {trde_able}주 중 {partial_sell_ratio}% = {sell_qty}주 매도"),
                    meta={"stage": 1, "cur_prc": cur, "partial_sell_ratio": float(partial_sell_ratio)},
                )
                return sig, used_refresh

        # ---- ② ATR 트레일링 청산(stage>=1, 또는 profit_pct 도달) --------- #
        if trail_mode == "profit_pct":
            trailing_active = rt is not None and Decimal(rt) >= trail_activate_pct
        else:  # after_partial
            trailing_active = stage >= 1

        if trailing_active:
            if trail_started_at is None:
                updates["trail_started_at"] = ctx.now

            if atr_date == today and atr_value is not None:
                pass  # 이미 오늘 계산됨 - 재사용(하루 1회 캐시)
            elif atr_budget_left <= 0:
                log.debug("%s: ATR 재계산 한도 도달 - 이전 값 유지(atr_date=%s)", stk_cd, atr_date)
            else:
                used_refresh = True
                bars = self._load_bars_for_atr(ctx, stk_cd, min_atr_bars)
                new_atr = wilder_atr(bars, atr_period) if len(bars) >= atr_period + 1 else None
                if new_atr is not None:
                    atr_value = new_atr
                    atr_date = today
                    updates["atr_value"] = atr_value
                    updates["atr_date"] = atr_date
                else:
                    log.debug("%s: ATR 계산용 일봉 부족(%d개, 필요 %d개 이상)",
                             stk_cd, len(bars), atr_period + 1)

            if atr_value is not None:
                stop = Decimal(peak_price) - Decimal(atr_value) * atr_multiplier
                if trail_floor_breakeven and pur > 0:
                    stop = max(stop, Decimal(pur))
                if Decimal(cur) <= stop:
                    sell_qty = int(h.get("trde_able_qty") or qty)
                    if sell_qty > 0:
                        self._persist(ctx, stk_cd, updates)
                        sig = Signal(
                            algo_code=self.code, stk_cd=stk_cd, stk_nm=h.get("stk_nm"),
                            side="SELL", qty=sell_qty, price=None, trde_tp="3",
                            kind=KIND_TAKE_PROFIT, score=float(rt) if rt is not None else None,
                            reason=(f"ATR 트레일링 청산 (peak {peak_price:,} - "
                                    f"ATR {atr_value:.2f}x{atr_multiplier} = {stop:,.0f} "
                                    f">= 현재 {cur:,})"),
                            meta={"stage": 2, "peak": peak_price, "atr": float(atr_value),
                                 "stop": int(stop)},
                        )
                        return sig, used_refresh

        self._persist(ctx, stk_cd, updates)
        return None, used_refresh

    # ------------------------------------------------------------------ #
    def _persist(self, ctx, stk_cd: str, updates: dict) -> None:
        if not updates:
            return
        try:
            ctx.db.upsert_position_exit_state(ctx.account_id, stk_cd, **updates)
        except Exception:  # noqa: BLE001 - 기록 실패가 평가/신호 생성을 막지 않는다
            log.warning("position_exit_state 갱신 실패 %s", stk_cd, exc_info=True)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _load_bars_for_atr(ctx, stk_cd: str, min_bars: int) -> list[dict]:
        """DB 저장분(`price_daily`) 우선, 부족하면 `ctx.market.daily_bars()` 로 보강.

        당일(아직 미확정) 봉은 뺀다(volatility_breakout 과 같은 기준).
        """
        def _drop_incomplete(bars: list[dict]) -> list[dict]:
            if bars and bars[-1].get("dt") and bars[-1]["dt"] >= ctx.now.date():
                return bars[:-1]
            return bars

        try:
            bars = _drop_incomplete(list(ctx.db.recent_bars(stk_cd, min_bars + 5)))
        except Exception:  # noqa: BLE001
            log.debug("recent_bars 조회 실패(ATR) %s", stk_cd, exc_info=True)
            bars = []
        if len(bars) >= min_bars or ctx.market is None:
            return bars
        try:
            fresh = _drop_incomplete(list(ctx.market.daily_bars(stk_cd)))
        except Exception:  # noqa: BLE001
            log.debug("일봉 조회 실패(ATR) %s", stk_cd, exc_info=True)
            return bars
        return fresh if len(fresh) > len(bars) else bars
