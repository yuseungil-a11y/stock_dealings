"""trend_position — 중장기 추세추종 신규 진입.

* 진입 조건(둘 다 충족):
  1. 최근 일봉 종가 기준 `ma_short_period`(기본 60)일 단순이동평균(SMA)이
     `ma_long_period`(기본 120)일 SMA 보다 크다(추세 레짐).
  2. 최근 `confirm_days`(기본 3)개 일봉 종가가 **각 시점의** 장기 SMA 보다 모두 높다
     (단발성 돌파가 아니라 장기선 위 안착 확인).
* macd_cross 처럼 "교차가 일어난 그 순간"만 잡지 않고, 매 평가주기마다 "지금 이 상태인가"를
  확인한다(의도적 설계) - 서버 재시작·다운타임으로 교차 당일을 놓쳐도 진입 기회를 잃지 않는다.
* 대상 종목은 `universe_filter` 와 **같은 로직**을 자기 자신의 파라미터로 재사용한다
  (macd_cross 와 동일 - universe_filter 알고리즘 자체의 on/off 와 무관하게 독립 동작).
  종목마스터가 비었거나 오래되면 `load_universe()` 의 fail-closed 판단을 그대로 따른다.
* 매수 직전 안전판(H2): 진입 조건을 충족해도 청산 판정 `trend_position_exit.exit_reason()`
  (청산 알고리즘의 현재 파라미터, 확정 일봉만 사용)이 매도 사유를 내면 그 종목은 사지 않는다.
  예) MA120 위 눌림목이지만 최근 10거래일 최저가 아래 → 사자마자 다음 주기 트레일링 매도되는 반복 방지.
* 이미 보유(`ctx.holdings`) 중이거나 거래불가(`ctx.untradable_reason`) 종목은 제외한다.
* 일 신규진입 한도(`max_new_per_day`)를 넘지 않는다. 후보가 한도보다 많으면 추세 강도
  ((단기SMA-장기SMA)/장기SMA) 가 큰 순으로 상한만큼만 신호를 만든다.
* 청산은 `trend_position_exit`(추세이탈·트레일링)가 전담하며, take_profit 은 이 알고리즘으로
  진입한 종목을 건드리지 않는다. risk_guard 손절은 그대로 적용된다(안전장치).

파라미터(seed.sql): ma_short_period, ma_long_period, confirm_days, buy_amount, max_new_per_day,
top_n, min_price, max_price, min_market_cap_eok, use_kospi, use_kosdaq, use_etf,
exclude_preferred, exclude_spac, exclude_warning, stale_days, order_type
"""
from __future__ import annotations

import logging

from .base import KIND_ENTRY, Algorithm, Signal, order_price, qty_for_amount
from .params import ParamSet
from .registry import register
from .universe_filter import APPLY_ENTRY, SCOPE_PER_MARKET, UniverseOptions, load_universe

log = logging.getLogger(__name__)

CODE = "trend_position"


# ====================================================================== #
# 순수 계산 함수 (테스트 가능)
# ====================================================================== #
def sma(values: list[float], period: int) -> list[float | None]:
    """단순이동평균. 처음 period-1개는 None, period번째부터 값 존재."""
    n = len(values)
    out: list[float | None] = [None] * n
    if period <= 0 or n < period:
        return out
    window = sum(values[:period])
    out[period - 1] = window / period
    for i in range(period, n):
        window += values[i] - values[i - period]
        out[i] = window / period
    return out


def trend_entry_state(closes: list[float], short: int, long: int,
                      confirm_days: int) -> dict | None:
    """진입 판단에 필요한 값 계산. 계산 불가(데이터 부족·기간 오류)면 None.

    반환: {"ma_short", "ma_long", "days_above", "regime_ok", "confirm_ok", "ok"}
    `days_above` 는 최근 confirm_days 개 봉 중 그 시점 장기 SMA 보다 종가가 높았던 봉 수.
    """
    if short <= 0 or long <= 0 or confirm_days <= 0 or short >= long:
        return None
    if len(closes) < long + confirm_days - 1:
        return None
    ma_s = sma(closes, short)
    ma_l = sma(closes, long)
    last_s, last_l = ma_s[-1], ma_l[-1]
    if last_s is None or last_l is None:
        return None
    days_above = 0
    for i in range(1, confirm_days + 1):
        m = ma_l[-i]
        if m is None:
            return None
        if closes[-i] > m:
            days_above += 1
    regime_ok = last_s > last_l
    confirm_ok = days_above == confirm_days
    return {"ma_short": last_s, "ma_long": last_l, "days_above": days_above,
            "regime_ok": regime_ok, "confirm_ok": confirm_ok,
            "ok": regime_ok and confirm_ok}


# ====================================================================== #
@register
class TrendPosition(Algorithm):
    code = CODE
    role = "entry"
    name = "중장기 추세추종"

    @staticmethod
    def validate_params(params) -> list[str]:
        short = params.int("ma_short_period", 60)
        long = params.int("ma_long_period", 120)
        if short >= long:
            return [f"단기 이동평균 기간({short})은 장기 이동평균 기간({long})보다 작아야 합니다."]
        return []

    def evaluate(self, ctx) -> list[Signal]:
        if not ctx.market_open or ctx.market is None:
            return []

        short = self.params.int("ma_short_period", 60)
        long = self.params.int("ma_long_period", 120)
        confirm = self.params.int("confirm_days", 3)
        if short >= long:
            ctx.note(f"{self.code}: 단기({short}) >= 장기({long}) - 알고리즘 비활성")
            return []
        if confirm <= 0:
            ctx.note(f"{self.code}: confirm_days({confirm}) <= 0 - 알고리즘 비활성")
            return []

        opts = self._universe_options()
        uni, err = load_universe(ctx.db, opts, ctx.now)
        if uni is None:
            ctx.note(f"{self.code}: {err}")
            return []

        buy_amount = self.params.int("buy_amount", 400000)
        max_new = self.params.int("max_new_per_day", 2)
        trde_tp = self.params.str("order_type", "3") or "3"

        try:
            new_today = ctx.db.count_new_entries_today(ctx.account_id, self.code)
        except Exception:  # noqa: BLE001
            new_today = 0
        remaining = max(0, max_new - new_today) if max_new > 0 else 0
        if remaining <= 0:
            ctx.note(f"{self.code}: 일 신규 진입 한도 도달({new_today}/{max_new})")
            return []

        # 청산 알고리즘 모듈은 이 모듈(CODE, sma)을 import 하므로 순환 import 를 피해 여기서 가져온다.
        from . import trend_position_exit as _exit
        exit_ma, lookback = self._exit_params(ctx, _exit.CODE)
        exit_need = max(exit_ma, lookback) + 1

        need_bars = long + confirm
        candidates: list[dict] = []
        for entry in uni.passed_entries:
            code = entry.stk_cd
            if code in ctx.holdings:
                continue  # 이미 보유
            if ctx.untradable_reason(code, entry.stk_nm):
                continue  # R-06: 거래불가(상장폐지·정리매매·거래정지) 종목 제외

            bars = self._load_bars(ctx, code, need_bars)
            if len(bars) < need_bars:
                continue  # 일봉 부족
            closes = [int(b.get("cur_prc") or 0) for b in bars]
            if any(c <= 0 for c in closes):
                continue

            st = trend_entry_state(closes, short, long, confirm)
            if st is None or not st["ok"]:
                continue
            # H2 안전판: 청산(trend_position_exit) 기준으로 이미 매도 대상인 종목은 사지 않는다
            # (예: MA 위 눌림목이지만 최근 N거래일 최저가 아래 → 매수 직후 다음 주기에 트레일링 매도).
            # 청산과 같은 순수함수·같은 확정 일봉(당일 미확정 봉 제외)으로 판정한다.
            cbars = _exit.load_confirmed_bars(ctx, code, exit_need)
            why = _exit.exit_reason(_exit.confirmed_closes(cbars), _exit.confirmed_lows(cbars),
                                    closes[-1], exit_ma, lookback)
            if why is not None:
                ctx.note(f"{self.code}: {code} 진입 보류 - 청산 조건 해당({why})")
                continue
            candidates.append({
                "stk_cd": code, "stk_nm": entry.stk_nm, "cur_prc": closes[-1],
                "ma_short": st["ma_short"], "ma_long": st["ma_long"],
                "days_above": st["days_above"],
                "strength": (st["ma_short"] - st["ma_long"]) / st["ma_long"],
            })

        # 후보가 한도보다 많으면 추세 강도((단기-장기)/장기)가 큰 순으로 상한만큼만
        candidates.sort(key=lambda c: c["strength"], reverse=True)

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
                price=order_price(trde_tp, c["cur_prc"]),
                trde_tp=trde_tp,
                amount=qty * c["cur_prc"],
                kind=KIND_ENTRY,
                score=float(c["strength"]),
                reason=(f"중장기 추세 (MA{short} {c['ma_short']:,.0f} > MA{long} {c['ma_long']:,.0f}, "
                        f"최근 {confirm}일 연속 MA{long} 위 {c['days_above']}/{confirm}, "
                        f"현재가 {c['cur_prc']:,})"),
                meta={"cur_prc": c["cur_prc"], "ma_short": c["ma_short"], "ma_long": c["ma_long"]},
            ))
        if signals:
            log.info("%s: 후보 %d종목 중 %d건 신호", self.code, len(candidates), len(signals))
        return signals

    # ------------------------------------------------------------------ #
    def _universe_options(self) -> UniverseOptions:
        """universe_filter 와 같은 로직을 자기 자신의 파라미터로 재사용(macd_cross 와 동일)."""
        return UniverseOptions(
            use_kospi=self.params.bool("use_kospi", True),
            use_kosdaq=self.params.bool("use_kosdaq", True),
            use_etf=self.params.bool("use_etf", False),
            rank_scope=SCOPE_PER_MARKET,
            top_n=self.params.int("top_n", 200),
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
    def _exit_params(ctx, exit_code: str) -> tuple[int, int]:
        """청산 알고리즘(trend_position_exit)의 현재 파라미터(exit_ma_period, trail_lookback_days).

        진입 안전판이 실제 청산과 같은 기준을 쓰도록 DB 의 청산 알고리즘 파라미터를 읽는다
        (사이클 캐시 `ctx.algorithm_rows()`). 못 읽으면 seed 기본값(120, 10).
        """
        for row in ctx.algorithm_rows():
            if row.get("code") == exit_code:
                ps = ParamSet(row.get("param_defs") or [], row.get("params") or {})
                return ps.int("exit_ma_period", 120), ps.int("trail_lookback_days", 10)
        return 120, 10

    @staticmethod
    def _load_bars(ctx, stk_cd: str, limit: int) -> list[dict]:
        """ctx.market.daily_bars() 우선, 실패시 ctx.db.recent_bars() 대체 (macd_cross 와 동일)."""
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
