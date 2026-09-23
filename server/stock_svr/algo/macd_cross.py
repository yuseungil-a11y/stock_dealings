"""macd_cross — MACD 골든크로스(기본, 조건 없음) 신규 진입.

* 조건은 **단 하나**: MACD 라인이 Signal 라인을 아래에서 위로 교차(골든크로스)하는
  순간에만 매수한다. 제로선(0선) 필터 등 추가 조건은 없다.
* 대상 종목은 `universe_filter`(시가총액 상위 N, 코스피+코스닥+선택적 ETF)와 **같은 로직**을
  재사용하되, 자기 자신의 파라미터로 `UniverseOptions` 를 구성해 독립적으로 동작한다
  (universe_filter 알고리즘 자체가 켜져 있는지와 무관하다).
* 종목마스터가 비었거나 오래되면 `load_universe()` 가 fail-closed 로 `None` 을 돌려주고,
  이 알고리즘도 그 판단을 그대로 존중해 신규 매수를 만들지 않는다.
* 이미 보유(`ctx.holdings`) 중이거나 거래불가(`ctx.untradable_reason`) 종목은 제외한다.
* 일 신규진입 한도(`max_new_per_day`)를 넘지 않는다. 후보가 한도보다 많으면 히스토그램
  (MACD-Signal) 절대값이 큰 순으로 상한만큼만 신호를 만든다.

파라미터(seed.sql): fast_period, slow_period, signal_period, top_n, use_kospi, use_kosdaq,
use_etf, min_price, max_price, min_market_cap_eok, exclude_preferred, exclude_spac,
exclude_warning, stale_days, buy_amount, max_new_per_day, order_type
"""
from __future__ import annotations

import logging

from .base import KIND_ENTRY, Algorithm, Signal, qty_for_amount
from .registry import register
from .universe_filter import APPLY_ENTRY, SCOPE_PER_MARKET, UniverseOptions, load_universe

log = logging.getLogger(__name__)

# 일봉이 모자라 계산을 못 하는 상황을 피하려는 여유분 (slow+signal 만으로도 계산은 되지만
# 직전값·최신값 비교(골든크로스 판정)까지 안정적으로 하려면 여유를 둔다).
_BARS_MARGIN = 5


# ====================================================================== #
# 순수 계산 함수 (테스트 가능)
# ====================================================================== #
def ema(values: list[float], period: int) -> list[float | None]:
    """표준 지수이동평균. 처음 period-1개는 None, period번째부터 값 존재.

    초기값은 첫 `period`개의 단순이동평균(SMA)으로 시작하는 표준 EMA 공식을 쓴다.
    """
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    k = 2.0 / (period + 1)
    sma = sum(values[:period]) / period
    out[period - 1] = sma
    prev = sma
    for i in range(period, n):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def macd_series(closes: list[float], fast: int, slow: int,
                signal: int) -> tuple[list[float | None], list[float | None]]:
    """MACD 라인(EMA(fast)-EMA(slow))과 Signal 라인(MACD 의 EMA(signal))을 반환.

    필요한 최소 봉 수(slow+signal 이상)가 없으면 계산하지 않고 (빈 리스트, 빈 리스트).
    """
    if fast <= 0 or slow <= 0 or signal <= 0:
        return [], []
    n = len(closes)
    if n < slow + signal:
        return [], []

    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)

    macd_full: list[float | None] = [None] * n
    for i in range(n):
        f, s = ema_fast[i], ema_slow[i]
        if f is not None and s is not None:
            macd_full[i] = f - s

    offset: int | None = None
    valid_macd: list[float] = []
    for i, v in enumerate(macd_full):
        if v is not None:
            if offset is None:
                offset = i
            valid_macd.append(v)
    if offset is None:
        return [], []

    signal_valid = ema(valid_macd, signal)
    signal_full: list[float | None] = [None] * n
    for j, v in enumerate(signal_valid):
        signal_full[offset + j] = v
    return macd_full, signal_full


def golden_cross(macd: list[float | None], signal: list[float | None]) -> bool:
    """마지막 값 기준 '전일 MACD < Signal 이었다가 오늘 MACD >= Signal' 인지 판정.

    None 이 섞여 있으면(계산 불가) False.
    """
    if len(macd) < 2 or len(signal) < 2:
        return False
    m_prev, m_now = macd[-2], macd[-1]
    s_prev, s_now = signal[-2], signal[-1]
    if m_prev is None or m_now is None or s_prev is None or s_now is None:
        return False
    return m_prev < s_prev and m_now >= s_now


# ====================================================================== #
@register
class MacdCross(Algorithm):
    code = "macd_cross"
    role = "entry"
    name = "MACD 골든크로스"

    @staticmethod
    def validate_params(params) -> list[str]:
        fast_p = params.int("fast_period", 12)
        slow_p = params.int("slow_period", 26)
        if fast_p >= slow_p:
            return [f"MACD 단기 EMA 기간({fast_p})은 장기 EMA 기간({slow_p})보다 작아야 합니다."]
        return []

    def evaluate(self, ctx) -> list[Signal]:
        if not ctx.market_open or ctx.market is None:
            return []

        fast_p = self.params.int("fast_period", 12)
        slow_p = self.params.int("slow_period", 26)
        signal_p = self.params.int("signal_period", 9)
        if fast_p >= slow_p:
            ctx.note(f"{self.code}: 단기({fast_p}) >= 장기({slow_p}) - 알고리즘 비활성")
            return []

        opts = self._universe_options()
        uni, err = load_universe(ctx.db, opts, ctx.now)
        if uni is None:
            ctx.note(f"{self.code}: {err}")
            return []

        buy_amount = self.params.int("buy_amount", 100000)
        max_new = self.params.int("max_new_per_day", 3)
        trde_tp = self.params.str("order_type", "3") or "3"

        try:
            new_today = ctx.db.count_new_entries_today(ctx.account_id, self.code)
        except Exception:  # noqa: BLE001
            new_today = 0
        remaining = max(0, max_new - new_today) if max_new > 0 else 0
        if remaining <= 0:
            ctx.note(f"{self.code}: 일 신규 진입 한도 도달({new_today}/{max_new})")
            return []

        need_bars = slow_p + signal_p + _BARS_MARGIN
        candidates: list[dict] = []
        for entry in uni.passed_entries:
            code = entry.stk_cd
            if code in ctx.holdings:
                continue  # 이미 보유 → 물타기 알고리즘 담당
            if ctx.untradable_reason(code, entry.stk_nm):
                continue  # R-06: 거래불가(상장폐지·정리매매·거래정지) 종목 제외

            bars = self._load_bars(ctx, code, need_bars)
            if len(bars) < slow_p + signal_p:
                continue  # 직전값·최신값 비교에 필요한 데이터 부족
            closes = [int(b.get("cur_prc") or 0) for b in bars]
            if any(c <= 0 for c in closes):
                continue

            macd, signal = macd_series(closes, fast_p, slow_p, signal_p)
            if not macd or not signal or not golden_cross(macd, signal):
                continue

            candidates.append({
                "stk_cd": code, "stk_nm": entry.stk_nm, "cur_prc": closes[-1],
                "macd": macd[-1], "signal": signal[-1],
            })

        # 후보가 한도보다 많으면 히스토그램(MACD-Signal) 절대값이 큰 순으로 상한만큼만
        candidates.sort(key=lambda c: abs(c["macd"] - c["signal"]), reverse=True)

        signals: list[Signal] = []
        for c in candidates:
            if len(signals) >= remaining:
                break
            qty = qty_for_amount(buy_amount, c["cur_prc"])
            if qty <= 0:
                continue
            signals.append(Signal(
                algo_code=self.code,
                stk_cd=c["stk_cd"],
                stk_nm=c["stk_nm"],
                side="BUY",
                qty=qty,
                price=None if trde_tp == "3" else c["cur_prc"],
                trde_tp=trde_tp,
                amount=qty * c["cur_prc"],
                kind=KIND_ENTRY,
                score=float(c["macd"] - c["signal"]),
                reason=(f"MACD 골든크로스 (MACD {c['macd']:.1f} > Signal {c['signal']:.1f})"),
                meta={"cur_prc": c["cur_prc"]},
            ))
        if signals:
            log.info("%s: 후보 %d종목 중 %d건 신호", self.code, len(candidates), len(signals))
        return signals

    # ------------------------------------------------------------------ #
    def _universe_options(self) -> UniverseOptions:
        """universe_filter 와 같은 로직을 자기 자신의 파라미터로 재사용."""
        return UniverseOptions(
            use_kospi=self.params.bool("use_kospi", True),
            use_kosdaq=self.params.bool("use_kosdaq", True),
            use_etf=self.params.bool("use_etf", False),
            rank_scope=SCOPE_PER_MARKET,
            top_n=self.params.int("top_n", 100),
            min_market_cap_eok=self.params.int("min_market_cap_eok", 0),
            min_price=self.params.int("min_price", 10000),
            max_price=self.params.int("max_price", 0),
            exclude_preferred=self.params.bool("exclude_preferred", True),
            exclude_spac=self.params.bool("exclude_spac", True),
            exclude_warning=self.params.bool("exclude_warning", True),
            apply_to=APPLY_ENTRY,
            stale_days=self.params.int("stale_days", 5),
        )

    @staticmethod
    def _load_bars(ctx, stk_cd: str, limit: int) -> list[dict]:
        """ctx.market.daily_bars() 우선, 실패시 ctx.db.recent_bars() 대체."""
        if ctx.market is not None:
            try:
                bars = list(ctx.market.daily_bars(stk_cd))
                if bars:
                    return bars[-limit:]
            except Exception:  # noqa: BLE001 - 조회 실패는 DB 저장분으로 대체
                log.debug("일봉 조회 실패 %s", stk_cd, exc_info=True)
        try:
            return list(ctx.db.recent_bars(stk_cd, limit))
        except Exception:  # noqa: BLE001
            return []
