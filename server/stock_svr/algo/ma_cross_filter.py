"""ma_cross_filter — 단기 이동평균이 장기 아래면 신규 진입을 차단하는 보조 필터."""
from __future__ import annotations

import logging

from .base import KIND_AVG_DOWN, KIND_ENTRY, Algorithm, Signal
from .registry import register

log = logging.getLogger(__name__)


def moving_average(bars: list[dict], period: int, offset: int = 0) -> float | None:
    """종가(cur_prc) 기준 이동평균. offset=1 이면 한 봉 전 기준."""
    if period <= 0:
        return None
    end = len(bars) - offset
    if end < period:
        return None
    window = bars[end - period:end]
    vals = [int(b.get("cur_prc") or 0) for b in window]
    if any(v <= 0 for v in vals):
        return None
    return sum(vals) / float(period)


@register
class MaCrossFilter(Algorithm):
    code = "ma_cross_filter"
    role = "filter"
    name = "이동평균 크로스 필터"

    def filter_signals(self, ctx, signals: list[Signal]) -> list[Signal]:
        if not signals or ctx.market is None:
            return signals
        short_p = self.params.int("short_period", 5)
        long_p = self.params.int("long_period", 20)
        mode = self.params.str("block_mode", "below") or "below"
        if short_p >= long_p:
            ctx.note(f"{self.code}: 단기({short_p}) >= 장기({long_p}) - 필터 비활성")
            return signals

        kept: list[Signal] = []
        for sig in signals:
            if sig.side != "BUY" or sig.kind not in (KIND_ENTRY, KIND_AVG_DOWN):
                kept.append(sig)
                continue
            blocked, detail = self._blocked(ctx, sig.stk_cd, short_p, long_p, mode)
            if blocked:
                self._log_block(ctx, sig, detail)
                continue
            kept.append(sig)
        return kept

    # ------------------------------------------------------------------ #
    def _blocked(self, ctx, stk_cd: str, short_p: int, long_p: int, mode: str) -> tuple[bool, str]:
        try:
            bars = ctx.market.daily_bars(stk_cd)
        except Exception:  # noqa: BLE001
            log.debug("일봉 조회 실패 %s", stk_cd, exc_info=True)
            return False, ""
        if len(bars) < long_p + 1:
            return False, ""
        s_now = moving_average(bars, short_p)
        l_now = moving_average(bars, long_p)
        if s_now is None or l_now is None:
            return False, ""
        if mode == "cross_down":
            s_prev = moving_average(bars, short_p, offset=1)
            l_prev = moving_average(bars, long_p, offset=1)
            if s_prev is None or l_prev is None:
                return False, ""
            if s_prev >= l_prev and s_now < l_now:
                return True, f"데드크로스 발생 (MA{short_p} {s_now:.1f} < MA{long_p} {l_now:.1f})"
            return False, ""
        if s_now < l_now:
            return True, f"MA{short_p}({s_now:.1f}) < MA{long_p}({l_now:.1f}) 하락국면"
        return False, ""

    def _log_block(self, ctx, sig: Signal, detail: str) -> None:
        log.info("%s 차단: %s %s", self.code, sig.stk_cd, detail)
        try:
            ctx.db.insert_signal(ctx.run_id, self.code, sig.stk_cd, sig.stk_nm, "BLOCK",
                                 None, f"{sig.algo_code} 신호 차단 - {detail}")
        except Exception:  # noqa: BLE001
            log.debug("BLOCK 신호 기록 실패", exc_info=True)
