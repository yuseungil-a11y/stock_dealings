"""risk_guard — 항상 켜져 있는 전역 리스크 한도.

* 모든 주문 신호는 `check()` 를 통과해야 Executor 로 간다.
* `evaluate()` 는 손절선에 닿은 보유종목의 전량 매도 신호를 만든다.

파라미터(seed.sql): max_total_invest, max_invest_per_stock, stop_loss_pct,
daily_loss_limit_pct, max_orders_per_day, trade_start_time, trade_end_time, exchange
"""
from __future__ import annotations

import logging
from decimal import Decimal

from ..util import today_kst
from .base import KIND_STOP_LOSS, SLIPPAGE_BUFFER, Algorithm, Signal, amount_with_buffer
from .registry import register

log = logging.getLogger(__name__)

__all__ = ["RiskGuard", "SLIPPAGE_BUFFER", "effective_limit"]


def effective_limit(abs_won: int, pct: Decimal, total_asset: int,
                    scope: str = "") -> tuple[int, str]:
    """유효 투입 한도 = min(절대한도(원), 총자산 × 비중%).

    반환: (한도(원), 어느 쪽이 적용됐는지 설명 문자열).
    비중 한도가 더 작으면 "비중 한도(10% = 1,234원)", 아니면 "절대 한도(300,000원)".
    """
    pct_limit = int(Decimal(total_asset) * Decimal(pct) / Decimal(100))
    if pct_limit <= abs_won:
        return pct_limit, f"비중 한도({_fmt_pct(pct)}% = {pct_limit:,}원)"
    return abs_won, f"절대 한도({abs_won:,}원)"


def _fmt_pct(pct: Decimal) -> str:
    s = format(Decimal(pct), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


@register
class RiskGuard(Algorithm):
    code = "risk_guard"
    role = "risk"
    name = "리스크 가드(전역 한도)"

    # ------------------------------------------------------------------ #
    @property
    def exchange(self) -> str:
        return self.params.str("exchange", "KRX") or "KRX"

    def in_trade_window(self, ctx) -> bool:
        start = self.params.time("trade_start_time")
        end = self.params.time("trade_end_time")
        t = ctx.now.time()
        if start and t < start:
            return False
        if end and t > end:
            return False
        return True

    # ------------------------------------------------------------------ #
    def evaluate(self, ctx) -> list[Signal]:
        """손절선 도달 종목 전량 매도."""
        signals: list[Signal] = []
        if not ctx.market_open:
            return signals
        stop_pct = self.params.dec("stop_loss_pct", -15)
        for stk_cd, h in ctx.holdings.items():
            qty = int(h.get("rmnd_qty") or 0)
            if qty <= 0:
                continue
            # R-06: 상장폐지·정리매매 종목은 손절 신호를 만들지 않는다(주문이 거부되며
            # 무한 재시도의 원인이 된다). API 가 수익률 0 을 주는 '우연'에 기대지 않는다.
            if ctx.untradable_reason(stk_cd, h.get("stk_nm")):
                continue
            rt = ctx.holding_profit_rate(stk_cd)
            if rt is None:
                continue
            if Decimal(rt) <= stop_pct:
                sell_qty = int(h.get("trde_able_qty") or qty)
                if sell_qty <= 0:
                    continue
                signals.append(Signal(
                    algo_code=self.code,
                    stk_cd=stk_cd,
                    stk_nm=h.get("stk_nm"),
                    side="SELL",
                    qty=sell_qty,
                    price=None,
                    trde_tp="3",
                    kind=KIND_STOP_LOSS,
                    score=float(rt),
                    exchange=self.exchange,
                    reason=f"손절선 도달 (수익률 {rt}% <= {stop_pct}%) 전량 매도",
                ))
        return signals

    # ------------------------------------------------------------------ #
    def check(self, ctx, signal: Signal) -> tuple[bool, str]:
        """주문 사전 검증. (통과여부, 차단사유)"""
        signal.exchange = signal.exchange or self.exchange

        if not ctx.market_open:
            return False, "장 시간이 아님"

        # R-06: 거래불가 종목은 매수·매도 어느 쪽도 주문하지 않는다
        untradable = ctx.untradable_reason(signal.stk_cd, signal.stk_nm)
        if untradable:
            return False, untradable

        if signal.side == "SELL":
            h = ctx.holding(signal.stk_cd)
            if not h or int(h.get("rmnd_qty") or 0) <= 0:
                return False, "보유수량 없음"
            avail = int(h.get("trde_able_qty") or h.get("rmnd_qty") or 0)
            if signal.qty > avail:
                signal.qty = avail
            if signal.qty <= 0:
                return False, "매매가능수량 0"
            return True, ""

        # ---- 매수 ---- #
        if signal.kind != KIND_STOP_LOSS and not self.in_trade_window(ctx):
            return False, (f"매매시간 외 (허용 {self.params.raw('trade_start_time')}"
                           f"~{self.params.raw('trade_end_time')})")

        # 일 주문 횟수: 실제 전송된 주문만 센다(B3 - 관찰 신호가 한도를 소진하지 않게)
        max_orders = self.params.int("max_orders_per_day", 30)
        if max_orders <= 0:
            return False, "일 최대 주문 횟수가 0 - 주문 금지"
        try:
            sent_today = ctx.db.count_orders_today(ctx.account_id, only_sent=True)
        except Exception as exc:  # noqa: BLE001 - 판단 불가 → 차단(S-06 fail-closed)
            ctx.halt(f"일 주문횟수 조회 실패({type(exc).__name__})")
            return False, f"일 주문횟수 조회 실패로 차단({type(exc).__name__})"
        if sent_today >= max_orders:
            return False, f"일 최대 주문 횟수 초과 ({sent_today}/{max_orders})"

        # 총자산(비중 한도·일 손실률의 기준값). 확인 불가면 여기서 차단한다(S-07/R-16).
        asset, asset_err = ctx.total_asset()
        if asset_err:
            ctx.halt(asset_err)
            return False, f"비중 한도 계산 불가로 차단 ({asset_err})"

        # 일 손실 한도: 0 이상은 오설정으로 보고 차단(S-04/S-05)
        loss_limit = self.params.dec("daily_loss_limit_pct", -3)
        if loss_limit >= 0:
            return False, f"일 손실 한도 설정 오류({loss_limit}) - 음수여야 합니다"
        rate, rate_err = self._daily_loss_rate(ctx)
        if rate_err:
            ctx.halt(rate_err)
            return False, f"일 손실률 확인 불가로 차단 ({rate_err})"
        if rate is not None and rate <= loss_limit:
            return False, f"일 손실 한도 도달 ({rate:.2f}% <= {loss_limit}%)"

        amount = self._amount_with_buffer(signal)
        if amount <= 0:
            return False, "주문금액을 산출할 수 없음"

        # 한도 0 = '무제한'이 아니라 **주문 금지**로 해석한다(S-04)
        max_total_abs = self.params.int("max_total_invest", 0)
        if max_total_abs <= 0:
            return False, "총 투입 한도가 0 - 주문 금지"
        max_per_stock_abs = self.params.int("max_invest_per_stock", 0)
        if max_per_stock_abs <= 0:
            return False, "종목당 최대 투입금이 0 - 주문 금지"

        # 유효 한도 = min(절대한도(원), 총자산 × 비중%)
        total_limit, total_desc = effective_limit(
            max_total_abs, self.params.dec("max_total_invest_pct", 100), asset, "총")
        if (ctx.total_invested() + amount) > total_limit:
            return False, (f"총 {total_desc} 초과 (기존 {ctx.total_invested():,} + "
                           f"{amount:,} > {total_limit:,}원)")

        per_limit, per_desc = effective_limit(
            max_per_stock_abs, self.params.dec("max_invest_per_stock_pct", 100), asset, "종목당")
        if (ctx.invested_in(signal.stk_cd) + amount) > per_limit:
            return False, (f"종목당 {per_desc} 초과 (기존 {ctx.invested_in(signal.stk_cd):,} + "
                           f"{amount:,} > {per_limit:,}원)")

        cash = ctx.cash_available()
        if cash <= 0:
            return False, "주문가능금액 확인 불가(0)"
        if amount > cash:
            return False, f"주문가능금액 부족 ({amount:,} > {cash:,})"

        st = ctx.position(signal.stk_cd)
        if st.get("stopped"):
            return False, "손절 후 재진입 금지 종목"

        return True, ""

    # ------------------------------------------------------------------ #
    def _amount_with_buffer(self, signal: Signal) -> int:
        """시장가 주문은 슬리피지 버퍼를 얹어 한도를 보수적으로 검사한다(S-12).

        Executor 의 사이클 누적(R-12)과 같은 산식을 쓰도록 algo.base 로 옮겼다.
        """
        return amount_with_buffer(signal)

    def _daily_loss_rate(self, ctx) -> tuple[Decimal | None, str]:
        """당일 손실률(%) 과 오류 사유. 조회 실패는 (None, 사유) 로 돌려 차단한다(S-06)."""
        try:
            realized = ctx.db.today_realized_pl(ctx.account_id, today_kst())
        except Exception as exc:  # noqa: BLE001
            return None, f"당일 실현손익 조회 실패({type(exc).__name__})"
        # 장중에는 daily_trade_summary 가 비어 있을 수 있어 체결 기반 추정을 더한다(B6)
        intraday = 0
        getter = getattr(ctx.db, "today_realized_pl_from_executions", None)
        if callable(getter):
            try:
                intraday = getter(ctx.account_id, today_kst())
            except Exception as exc:  # noqa: BLE001
                return None, f"당일 체결 손익 추정 실패({type(exc).__name__})"
        pl = realized if abs(realized) >= abs(intraday) else intraday
        base = ctx.balance.get("prsm_dpst_aset_amt") or ctx.balance.get("tot_evlt_amt") or 0
        try:
            base = int(base or 0)
        except (TypeError, ValueError):
            base = 0
        if base <= 0:
            # R-16: 판정 생략(=통과)이 아니라 **명시적 차단 사유**로 돌려준다.
            # 기준 자산을 모르면 일 손실 한도가 아예 작동하지 않으므로 fail-closed 가 맞다.
            return None, "일 손실률 기준 자산 확인 불가 (추정예탁자산·총평가금액 0)"
        return (Decimal(pl) / Decimal(base)) * 100, ""
