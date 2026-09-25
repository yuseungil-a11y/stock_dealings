"""averaging_down — 분할매수(물타기).

평단(마지막 매수가) 대비 drop_pct% 하락 시 추가매수.
`position_state.avg_down_count` 가 max_steps 를 넘으면 더 이상 추가하지 않는다(**무한 물타기 불가**).
손절선(risk_guard.stop_loss_pct) 도달 시에는 추가매수 대신 전량 매도 신호를 낸다.
"""
from __future__ import annotations

import datetime as _dt
import logging
from decimal import Decimal

from .base import KIND_AVG_DOWN, KIND_STOP_LOSS, Algorithm, Signal, qty_for_amount
from .registry import register
from .risk_guard import stop_loss_pct as guard_stop_loss_pct

log = logging.getLogger(__name__)


@register
class AveragingDown(Algorithm):
    code = "averaging_down"
    role = "risk"
    name = "분할매수(물타기)"

    # ------------------------------------------------------------------ #
    def evaluate(self, ctx) -> list[Signal]:
        signals: list[Signal] = []
        if not ctx.market_open:
            return signals

        drop_pct = self.params.dec("drop_pct", 10)
        step_amount = self.params.int("step_buy_amount", 100000)
        max_steps = self.params.int("max_steps", 3)
        cooldown_min = self.params.int("cooldown_min", 30)
        trde_tp = self.params.str("order_type", "3") or "3"
        stop_pct = guard_stop_loss_pct(ctx)

        for stk_cd, h in ctx.holdings.items():
            qty_held = int(h.get("rmnd_qty") or 0)
            if qty_held <= 0:
                continue
            # R-06: 상장폐지·정리매매 종목은 물타기도 손절도 하지 않는다
            if ctx.untradable_reason(stk_cd, h.get("stk_nm")):
                continue
            cur = int(h.get("cur_prc") or 0)
            if cur <= 0:
                cur = ctx.current_price(stk_cd) or 0
            if cur <= 0:
                continue

            st = ctx.position(stk_cd)
            steps = int(st.get("avg_down_count") or 0)
            base_price = int(st.get("last_buy_price") or h.get("pur_pric") or 0)
            if base_price <= 0:
                continue

            rt = ctx.holding_profit_rate(stk_cd)

            # 손절선 도달: 추가매수 중단하고 전량 매도
            if rt is not None and Decimal(rt) <= stop_pct:
                sell_qty = int(h.get("trde_able_qty") or qty_held)
                if sell_qty > 0:
                    signals.append(Signal(
                        algo_code=self.code, stk_cd=stk_cd, stk_nm=h.get("stk_nm"),
                        side="SELL", qty=sell_qty, price=None, trde_tp="3",
                        kind=KIND_STOP_LOSS, score=float(rt),
                        reason=f"물타기 중단·손절 (수익률 {rt}% <= {stop_pct}%) 전량 매도",
                    ))
                continue

            if max_steps <= 0 or steps >= max_steps:
                continue

            threshold = Decimal(base_price) * (Decimal(1) - drop_pct / Decimal(100))
            if Decimal(cur) > threshold:
                continue

            # 이미 익절(take_profit) 부분매도 이상 진행된 포지션에는 물타기를 하지 않는다
            # (하나는 손실 확대 방지, 하나는 이익 확정 - 두 전략이 같은 포지션에서 충돌한다)
            try:
                exit_st = ctx.db.get_position_exit_state(ctx.account_id, stk_cd)
            except Exception:  # noqa: BLE001 - 조회 실패는 물타기를 막지 않는다(기존 동작 유지)
                log.debug("position_exit_state 조회 실패(물타기 판단) %s", stk_cd, exc_info=True)
                exit_st = None
            if exit_st and int(exit_st.get("tp_stage") or 0) > 0:
                continue

            if cooldown_min > 0:
                last = st.get("updated_at")
                if isinstance(last, _dt.datetime) and \
                        (ctx.now - last) < _dt.timedelta(minutes=cooldown_min):
                    continue

            qty = qty_for_amount(step_amount, cur)
            if qty <= 0:
                continue
            signals.append(Signal(
                algo_code=self.code, stk_cd=stk_cd, stk_nm=h.get("stk_nm"),
                side="BUY", qty=qty, price=None if trde_tp == "3" else cur,
                trde_tp=trde_tp, amount=qty * cur, kind=KIND_AVG_DOWN,
                score=float(rt) if rt is not None else None,
                reason=(f"물타기 {steps + 1}/{max_steps}회차 "
                        f"(기준 {base_price:,} 대비 -{drop_pct}% 이하, 현재 {cur:,})"),
                meta={"step": steps + 1, "cur_prc": cur},
            ))
        return signals
