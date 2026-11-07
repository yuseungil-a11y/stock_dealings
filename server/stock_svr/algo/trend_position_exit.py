"""trend_position_exit — 중장기 추세추종(trend_position) 전용 청산 (추세이탈·트레일링).

* 대상: `position_state.entry_algo == 'trend_position'` 인 보유종목만. 다른 알고리즘이 진입한
  종목이나 수동매수 종목은 건드리지 않는다. 보유수량 0·거래불가 종목도 제외.
* 매도 조건(먼저 해당하는 쪽으로 **전량** 시장가 매도, kind=liquidate):
  1. 추세이탈: 현재가가 확정 일봉 종가 기준 `exit_ma_period`(기본 120)일 SMA 아래.
     퍼센트 고정 손절 대신 "추세 구조가 깨졌다"는 신호로 청산한다.
  2. 트레일링: 현재가가 최근 `trail_lookback_days`(기본 10)개 **확정** 일봉의 최저 저가(low_pric)
     아래. 상태 테이블 없이 매 평가주기마다 일봉에서 즉시 재계산한다(코드 단순성 우선).
     ※ 개선방향 문서(2026-11-07) 원안의 "주간 저점 트레일링"을 상태 저장 없이 구현하기 쉽게
       "최근 N거래일 최저가 롤링"으로 단순화한 것이다.
* 당일(장중, 미확정) 봉은 계산에서 뺀다 - 당일 저가를 포함하면 현재가가 그 저가 아래로 내려갈 수
  없어 트레일링이 영원히 발동하지 않는다.
* 판정 로직은 순수함수 `exit_reason()` 하나에 있고, trend_position(진입)도 매수 직전에 같은 함수로
  "이미 청산 조건에 걸린 종목"을 걸러낸다(사자마자 다음 주기 매도 → 재진입 반복 방지, H2).
* `check()` 는 따로 두지 않는다 - SELL 신호의 수량 보정·보유수량 확인은 risk_guard.check() 가
  공통으로 처리한다. risk_guard 의 손절(stop_loss_pct)은 이 종목에도 그대로 적용되며(안전장치),
  둘 중 먼저 조건을 충족한 쪽이 실제 매도를 실행한다.

파라미터(seed.sql): exit_ma_period, trail_lookback_days
"""
from __future__ import annotations

import logging

from .base import KIND_LIQUIDATE, Algorithm, Signal
from .registry import register
from .trend_position import CODE as ENTRY_CODE
from .trend_position import sma

log = logging.getLogger(__name__)

CODE = "trend_position_exit"


# ====================================================================== #
# 순수 계산 함수 (테스트 가능)
# ====================================================================== #
def _min_recent_low(lows: list[int], lookback: int) -> int | None:
    """최근 `lookback` 개 저가 중 최저값. 개수가 모자라거나 0 이하 값이 있으면 None."""
    if lookback <= 0 or len(lows) < lookback:
        return None
    recent = lows[-lookback:]
    if any(v <= 0 for v in recent):
        return None
    return min(recent)


def trailing_low(bars: list[dict], lookback: int) -> int | None:
    """최근 `lookback` 개 봉의 최저 저가. 봉이 모자라거나 저가가 없으면 None."""
    return _min_recent_low([int(b.get("low_pric") or 0) for b in bars], lookback)


def exit_reason(closes_confirmed: list[int], lows_confirmed: list[int], cur_price: int,
                exit_ma_period: int, trail_lookback_days: int) -> str | None:
    """청산 판정(순수함수). 매도해야 하면 사유 문자열, 아니면 None.

    `closes_confirmed`/`lows_confirmed` 는 **확정 일봉만**(당일 미확정 봉 제외)의 종가/저가 목록
    (과거→최근 순). 이 함수는 청산(trend_position_exit)과 진입 직전 안전판(trend_position)이
    **함께** 쓰는 단일 판정 기준이다 - "사자마자 다음 주기에 팔리는" 진입을 막는다(H2).

    1. 추세이탈: cur_price < 확정 종가 `exit_ma_period` 일 SMA
    2. 트레일링: cur_price < 최근 `trail_lookback_days` 개 확정 일봉 최저 저가
    둘 다 **미만**일 때만 매도(같으면 매도하지 않음). 데이터가 모자란 항목은 판정하지 않는다.
    """
    if cur_price <= 0:
        return None
    closes = closes_confirmed
    if (exit_ma_period > 0 and len(closes) >= exit_ma_period
            and all(c > 0 for c in closes[-exit_ma_period:])):
        ma = sma(closes, exit_ma_period)[-1]
        if ma is not None and cur_price < ma:
            return f"추세이탈 청산 (현재가 {cur_price:,} < MA{exit_ma_period} {ma:,.0f})"
    low = _min_recent_low(lows_confirmed, trail_lookback_days)
    if low is not None and cur_price < low:
        return (f"트레일링 청산 (현재가 {cur_price:,} < 최근 {trail_lookback_days}거래일 "
                f"최저가 {low:,})")
    return None


# ====================================================================== #
@register
class TrendPositionExit(Algorithm):
    code = CODE
    role = "risk"
    name = "중장기 청산(추세이탈·트레일링)"

    def evaluate(self, ctx) -> list[Signal]:
        signals: list[Signal] = []
        if not ctx.market_open or ctx.market is None:
            return signals

        exit_ma = self.params.int("exit_ma_period", 120)
        lookback = self.params.int("trail_lookback_days", 10)

        for stk_cd, h in ctx.holdings.items():
            try:
                sig = self._evaluate_one(ctx, stk_cd, h, exit_ma=exit_ma, lookback=lookback)
            except Exception:  # noqa: BLE001 - 종목 하나의 오류가 다른 종목/알고리즘을 막지 않게
                log.exception("%s: 중장기 청산 평가 중 오류", stk_cd)
                continue
            if sig is not None:
                signals.append(sig)
        return signals

    # ------------------------------------------------------------------ #
    def _evaluate_one(self, ctx, stk_cd: str, h: dict, *, exit_ma: int,
                      lookback: int) -> Signal | None:
        qty = int(h.get("rmnd_qty") or 0)
        if qty <= 0:
            return None
        if ctx.position(stk_cd).get("entry_algo") != ENTRY_CODE:
            return None  # trend_position 이 진입한 종목만 관리
        if ctx.untradable_reason(stk_cd, h.get("stk_nm")):
            return None  # R-06: 거래불가 종목은 청산 신호도 만들지 않는다

        cur = int(h.get("cur_prc") or 0)
        if cur <= 0:
            cur = ctx.current_price(stk_cd) or 0
        if cur <= 0:
            return None

        bars = self._load_bars(ctx, stk_cd, max(exit_ma, lookback) + 1)
        sell_qty = int(h.get("trde_able_qty") or qty)
        if sell_qty <= 0:
            return None

        # ① 추세이탈(현재가 < 장기 SMA) / ② 트레일링(현재가 < 최근 N거래일 최저 저가)
        # 판정은 진입 쪽(trend_position)과 공유하는 순수함수 exit_reason() 하나로 한다.
        reason = exit_reason(confirmed_closes(bars), confirmed_lows(bars), cur, exit_ma, lookback)
        if reason is None:
            return None
        return self._sell(stk_cd, h, sell_qty, cur, reason)

    def _sell(self, stk_cd: str, h: dict, qty: int, cur: int, reason: str) -> Signal:
        return Signal(
            algo_code=self.code, stk_cd=stk_cd, stk_nm=h.get("stk_nm"),
            side="SELL", qty=qty, price=None, trde_tp="3", kind=KIND_LIQUIDATE,
            reason=reason, meta={"cur_prc": cur},
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _load_bars(ctx, stk_cd: str, limit: int) -> list[dict]:
        return load_confirmed_bars(ctx, stk_cd, limit)


# ====================================================================== #
# 확정 일봉 로딩 (청산·진입 안전판 공용)
# ====================================================================== #
def confirmed_closes(bars: list[dict]) -> list[int]:
    return [int(b.get("cur_prc") or 0) for b in bars]


def confirmed_lows(bars: list[dict]) -> list[int]:
    return [int(b.get("low_pric") or 0) for b in bars]


def load_confirmed_bars(ctx, stk_cd: str, limit: int) -> list[dict]:
    """확정 일봉만(당일 미확정 봉 제외). ctx.market.daily_bars() 우선, 실패시 DB 저장분."""
    def _drop_incomplete(bars: list[dict]) -> list[dict]:
        if bars and bars[-1].get("dt") and bars[-1]["dt"] >= ctx.now.date():
            return bars[:-1]
        return bars

    if ctx.market is not None:
        try:
            bars = _drop_incomplete(list(ctx.market.daily_bars(stk_cd)))
            if bars:
                return bars[-limit:]
        except Exception:  # noqa: BLE001 - 조회 실패는 DB 저장분으로 대체
            log.debug("일봉 조회 실패 %s", stk_cd, exc_info=True)
    try:
        return _drop_incomplete(list(ctx.db.recent_bars(stk_cd, limit + 1)))[-limit:]
    except Exception:  # noqa: BLE001
        return []
