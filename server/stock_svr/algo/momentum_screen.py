"""momentum_screen — 등락률 상위(ka10027) ∩ 거래량 급증(ka10023) 교집합 진입."""
from __future__ import annotations

import logging
import re
from decimal import Decimal

from .base import KIND_ENTRY, Algorithm, Signal, qty_for_amount
from .registry import register

log = logging.getLogger(__name__)

# ETF/ETN/스팩 등 제외 패턴
_EXCLUDE_RE = re.compile(r"(KODEX|TIGER|KBSTAR|ARIRANG|HANARO|SOL |ACE |PLUS |RISE |ETN|스팩|리츠|우$|[0-9]+호)",
                         re.IGNORECASE)


@register
class MomentumScreen(Algorithm):
    code = "momentum_screen"
    role = "entry"
    name = "모멘텀 스크리닝"

    def evaluate(self, ctx) -> list[Signal]:
        if not ctx.market_open or ctx.market is None:
            return []

        market = self.params.str("market", "000") or "000"
        try:
            flu = ctx.market.rank_flu_rt(mrkt_tp=market)
            vol = ctx.market.volume_surge(mrkt_tp=market)
        except Exception:  # noqa: BLE001 - 조회 실패는 이번 주기 생략
            log.warning("모멘텀 후보 조회 실패", exc_info=True)
            return []

        # 조회 결과는 언제나 기록(관찰모드에서도 근거가 남도록)
        try:
            if flu:
                ctx.market.record_screening("ka10027", flu[:50])
            if vol:
                ctx.market.record_screening("ka10023", vol[:50])
        except Exception:  # noqa: BLE001
            log.debug("screening_result 기록 실패", exc_info=True)

        vol_by_code = {r["stk_cd"]: r for r in vol}
        min_flu = self.params.dec("min_flu_rt", 3)
        max_flu = self.params.dec("max_flu_rt", 15)
        min_surge = self.params.dec("min_volume_surge_rt", 100)
        min_qty = self.params.int("min_trde_qty", 0)
        min_price = self.params.int("min_price", 0)
        exclude_etf = self.params.bool("exclude_etf", True)
        top_n = max(1, self.params.int("top_n", 5))
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

        candidates: list[dict] = []
        for r in flu:
            code = r["stk_cd"]
            v = vol_by_code.get(code)
            if v is None:
                continue  # 교집합만
            rt = r.get("flu_rt")
            if rt is None or not (min_flu <= Decimal(rt) <= max_flu):
                continue
            surge = v.get("sdnin_rt")
            if surge is None or Decimal(surge) < min_surge:
                continue
            if min_qty and (v.get("now_trde_qty") or r.get("now_trde_qty") or 0) < min_qty:
                continue
            price = int(r.get("cur_prc") or v.get("cur_prc") or 0)
            if price <= 0 or (min_price and price < min_price):
                continue
            name = r.get("stk_nm") or v.get("stk_nm") or ""
            if exclude_etf and _EXCLUDE_RE.search(name):
                continue
            if code in ctx.holdings:
                continue  # 이미 보유 → 물타기 알고리즘 담당
            if ctx.untradable_reason(code, name):
                continue  # R-06: 거래불가(상장폐지·정리매매·거래정지) 종목 제외
            candidates.append({"stk_cd": code, "stk_nm": name, "cur_prc": price,
                               "flu_rt": Decimal(rt), "sdnin_rt": Decimal(surge)})

        candidates.sort(key=lambda c: (c["sdnin_rt"], c["flu_rt"]), reverse=True)

        signals: list[Signal] = []
        for c in candidates[:top_n]:
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
                score=float(c["sdnin_rt"]),
                reason=(f"등락률 {c['flu_rt']}% + 거래량급증 {c['sdnin_rt']}% 교집합 "
                        f"(현재가 {c['cur_prc']:,})"),
                meta={"cur_prc": c["cur_prc"]},
            ))
        if signals:
            log.info("%s: 후보 %d종목 중 %d건 신호", self.code, len(candidates), len(signals))
        return signals
