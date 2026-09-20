"""volatility_breakout — 당일 시가 + 전일 변동폭 × K 상향 돌파 시 매수, 지정 시각 청산."""
from __future__ import annotations

import logging
from decimal import Decimal

from .base import KIND_ENTRY, KIND_LIQUIDATE, Algorithm, Signal, qty_for_amount
from .registry import register

log = logging.getLogger(__name__)

MAX_WATCH = 20


def target_price(open_pric: int, prev_high: int, prev_low: int, k: float | Decimal) -> int:
    """돌파 목표가 = 당일 시가 + (전일 고가 - 전일 저가) × K"""
    rng = max(0, int(prev_high) - int(prev_low))
    return int(open_pric) + int(rng * float(k))


@register
class VolatilityBreakout(Algorithm):
    code = "volatility_breakout"
    role = "entry"
    name = "변동성 돌파"

    # ------------------------------------------------------------------ #
    def watch_list(self, ctx) -> list[str]:
        raw = self.params.str("watch_symbols", "").strip()
        if raw:
            return [c.strip() for c in raw.replace(";", ",").split(",") if c.strip()][:MAX_WATCH]
        # 비우면 보유종목 + 최근 스크리닝 후보
        codes = list(ctx.holdings)
        try:
            rows = ctx.db.query(
                "SELECT DISTINCT stk_cd FROM screening_result "
                "WHERE captured_at >= (NOW() - INTERVAL 1 DAY) ORDER BY stk_cd LIMIT %s", (MAX_WATCH,))
            codes += [r["stk_cd"] for r in rows]
        except Exception:  # noqa: BLE001
            log.debug("스크리닝 후보 조회 실패", exc_info=True)
        seen, out = set(), []
        for c in codes:
            if c and c not in seen:
                seen.add(c)
                out.append(c)
        return out[:MAX_WATCH]

    # ------------------------------------------------------------------ #
    def evaluate(self, ctx) -> list[Signal]:
        signals: list[Signal] = []
        if ctx.market is None:
            return signals

        # 청산 시각 도달 → 이 알고리즘으로 진입한 포지션 전량 매도
        liq_time = self.params.time("liquidate_time")
        if liq_time and ctx.now.time() >= liq_time:
            for stk_cd, h in ctx.holdings.items():
                st = ctx.position(stk_cd)
                if st.get("entry_algo") != self.code:
                    continue
                # R-06: 거래불가 종목은 청산 신호도 만들지 않는다(주문이 거부된다)
                if ctx.untradable_reason(stk_cd, h.get("stk_nm")):
                    continue
                qty = int(h.get("trde_able_qty") or h.get("rmnd_qty") or 0)
                if qty <= 0:
                    continue
                signals.append(Signal(
                    algo_code=self.code, stk_cd=stk_cd, stk_nm=h.get("stk_nm"),
                    side="SELL", qty=qty, price=None, trde_tp="3", kind=KIND_LIQUIDATE,
                    reason=f"청산 시각({liq_time.strftime('%H:%M')}) 도달 전량 청산",
                ))
            return signals

        if not ctx.market_open:
            return signals

        k = self.params.dec("k_value", 0.5)
        min_range_pct = self.params.dec("min_range_pct", 1.5)
        buy_amount = self.params.int("buy_amount", 100000)
        trde_tp = self.params.str("order_type", "3") or "3"

        for stk_cd in self.watch_list(ctx):
            if stk_cd in ctx.holdings:
                continue
            if ctx.untradable_reason(stk_cd):       # R-06: 거래불가 종목 신규진입 금지
                continue
            try:
                bars = ctx.market.daily_bars(stk_cd)
            except Exception:  # noqa: BLE001
                log.debug("일봉 조회 실패 %s", stk_cd, exc_info=True)
                continue
            if len(bars) < 2:
                continue
            prev = bars[-2] if bars[-1]["dt"] >= ctx.now.date() else bars[-1]
            prev_high = int(prev.get("high_pric") or 0)
            prev_low = int(prev.get("low_pric") or 0)
            prev_close = int(prev.get("cur_prc") or 0)
            if prev_high <= 0 or prev_low <= 0 or prev_close <= 0:
                continue
            range_pct = Decimal(prev_high - prev_low) / Decimal(prev_close) * 100
            if range_pct < min_range_pct:
                continue
            try:
                quote = ctx.market.quote(stk_cd)
            except Exception:  # noqa: BLE001
                continue
            if not quote or not quote.get("open_pric") or not quote.get("cur_prc"):
                continue
            tgt = target_price(quote["open_pric"], prev_high, prev_low, k)
            cur = int(quote["cur_prc"])
            if cur < tgt:
                continue
            qty = qty_for_amount(buy_amount, cur)
            if qty <= 0:
                continue
            signals.append(Signal(
                algo_code=self.code, stk_cd=stk_cd, stk_nm=quote.get("stk_nm"),
                side="BUY", qty=qty, price=None if trde_tp == "3" else cur,
                trde_tp=trde_tp, amount=qty * cur, kind=KIND_ENTRY, score=float(range_pct),
                reason=(f"변동성 돌파 (목표 {tgt:,} <= 현재 {cur:,}, K={k}, "
                        f"전일변동 {range_pct:.2f}%)"),
                meta={"target": tgt, "cur_prc": cur},
            ))
        return signals
