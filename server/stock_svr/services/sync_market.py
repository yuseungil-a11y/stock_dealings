"""시세/순위/종목마스터/테마 조회 (ka10099, ka10081, ka10027, ka10023, ka10001,
ka90001, ka90002) — 읽기 전용 TR."""
from __future__ import annotations

import datetime as _dt
import logging
import time

from ..db import Database
from ..kiwoom.parse import norm_stk_cd, rows, to_date, to_dec, to_int
from ..kiwoom.rest import KiwoomRest
from ..util import now_kst, today_kst

log = logging.getLogger(__name__)

MARKET_KOSPI = "0"
MARKET_KOSDAQ = "10"

# 순위 API 응답 캐시 수명(초) - 불필요한 실서버 호출을 줄인다
RANK_CACHE_SEC = 50
BARS_CACHE_SEC = 600
# 테마(ka90001/ka90002)는 하루 1회 트렌드 스캔에서만 쓰므로 길게 캐시한다
THEME_CACHE_SEC = 600


class MarketService:
    """시세 조회 + 캐시."""

    def __init__(self, db: Database, rest: KiwoomRest):
        self.db = db
        self.rest = rest
        self._cache: dict[str, tuple[float, object]] = {}

    # -- 캐시 ---------------------------------------------------------- #
    def _cached(self, key: str, ttl: float):
        hit = self._cache.get(key)
        if hit and (time.monotonic() - hit[0]) < ttl:
            return hit[1]
        return None

    def _store(self, key: str, value):
        self._cache[key] = (time.monotonic(), value)
        return value

    def clear_cache(self) -> None:
        self._cache.clear()

    # ------------------------------------------------------------------ #
    def sync_stock_master(self, markets: tuple[str, ...] = (MARKET_KOSPI, MARKET_KOSDAQ)) -> int:
        """ka10099 종목 리스트 → stock_master (하루 1회)."""
        total = 0
        for mrkt in markets:
            pages = self.rest.call_paged("ka10099", {"mrkt_tp": mrkt}, max_pages=20)
            batch: list[dict] = []
            for page in pages:
                for r in rows(page, "list"):
                    code = norm_stk_cd(r.get("code"))
                    if not code:
                        continue
                    batch.append({
                        "stk_cd": code,
                        "stk_nm": (r.get("name") or "").strip(),
                        "market_code": (r.get("marketCode") or "")[:10] or None,
                        "market_name": (r.get("marketName") or "")[:30] or None,
                        "up_name": (r.get("upName") or "")[:60] or None,
                        "list_count": to_int(r.get("listCount")),
                        "last_price": to_int(r.get("lastPrice")),
                        "state": (r.get("state") or "")[:100] or None,
                        "order_warning": (r.get("orderWarning") or "")[:10] or None,
                        "nxt_enable": (r.get("nxtEnable") or "")[:5] or None,
                        "reg_day": (r.get("regDay") or "")[:8] or None,
                    })
            total += self.db.upsert_stock_master(batch)
        log.info("종목마스터 갱신 %d건", total)
        self.clear_cache()
        self._invalidate_universe()
        return total

    @staticmethod
    def _invalidate_universe() -> None:
        """종목마스터가 바뀌었으므로 universe_filter 의 시총 순위 캐시를 버린다."""
        try:
            from ..algo.universe_filter import clear_universe_cache

            clear_universe_cache()
        except Exception:  # noqa: BLE001 - 캐시 무효화 실패가 동기화를 죽이지 않게
            log.debug("유니버스 캐시 무효화 실패", exc_info=True)

    # ------------------------------------------------------------------ #
    def rank_flu_rt(self, mrkt_tp: str = "000", stex_tp: str = "1") -> list[dict]:
        """ka10027 전일대비등락률상위."""
        key = f"ka10027:{mrkt_tp}:{stex_tp}"
        hit = self._cached(key, RANK_CACHE_SEC)
        if hit is not None:
            return hit  # type: ignore[return-value]
        body = {
            "mrkt_tp": mrkt_tp, "sort_tp": "1", "trde_qty_cnd": "0000", "stk_cnd": "0",
            "crd_cnd": "0", "updown_incls": "1", "pric_cnd": "0", "trde_prica_cnd": "0",
            "stex_tp": stex_tp,
        }
        data, _ = self.rest.call("ka10027", body)
        out = []
        for i, r in enumerate(rows(data, "pred_pre_flu_rt_upper"), start=1):
            code = norm_stk_cd(r.get("stk_cd"))
            if not code:
                continue
            out.append({
                "rank_no": i,
                "stk_cd": code,
                "stk_nm": (r.get("stk_nm") or "").strip(),
                "cur_prc": abs(to_int(r.get("cur_prc"), 0) or 0),
                "flu_rt": to_dec(r.get("flu_rt")),
                "now_trde_qty": to_int(r.get("now_trde_qty")),
                "sdnin_rt": None,
            })
        return self._store(key, out)  # type: ignore[return-value]

    def volume_surge(self, mrkt_tp: str = "000", stex_tp: str = "1") -> list[dict]:
        """ka10023 거래량급증."""
        key = f"ka10023:{mrkt_tp}:{stex_tp}"
        hit = self._cached(key, RANK_CACHE_SEC)
        if hit is not None:
            return hit  # type: ignore[return-value]
        body = {
            "mrkt_tp": mrkt_tp, "sort_tp": "1", "tm_tp": "2", "trde_qty_tp": "5",
            "tm": "", "stk_cnd": "0", "pric_tp": "0", "stex_tp": stex_tp,
        }
        data, _ = self.rest.call("ka10023", body)
        out = []
        for i, r in enumerate(rows(data, "trde_qty_sdnin"), start=1):
            code = norm_stk_cd(r.get("stk_cd"))
            if not code:
                continue
            out.append({
                "rank_no": i,
                "stk_cd": code,
                "stk_nm": (r.get("stk_nm") or "").strip(),
                "cur_prc": abs(to_int(r.get("cur_prc"), 0) or 0),
                "flu_rt": to_dec(r.get("flu_rt")),
                "now_trde_qty": to_int(r.get("now_trde_qty")),
                "sdnin_rt": to_dec(r.get("sdnin_rt")),
            })
        return self._store(key, out)  # type: ignore[return-value]

    def record_screening(self, source_api: str, items: list[dict]) -> int:
        return self.db.insert_screening(now_kst(), source_api, items)

    # -- 테마 (claude_trend_scan 전용, 읽기 전용) ------------------------ #
    def themes(self, flu_pl_amt_tp: str = "3", stex_tp: str = "1",
               date_tp: str = "10") -> list[dict]:
        """ka90001 테마그룹별요청. `flu_pl_amt_tp='3'` = 상위등락률 순.

        전체검색(`qry_tp='0'`)이라 첫 페이지만 받아도 상위 테마가 모두 들어온다
        (연속조회를 따라가지 않아 호출 1회로 끝난다).
        """
        key = f"ka90001:{flu_pl_amt_tp}:{stex_tp}:{date_tp}"
        hit = self._cached(key, THEME_CACHE_SEC)
        if hit is not None:
            return hit  # type: ignore[return-value]
        body = {"qry_tp": "0", "stk_cd": "", "date_tp": date_tp, "thema_nm": "",
                "flu_pl_amt_tp": flu_pl_amt_tp, "stex_tp": stex_tp}
        data, _ = self.rest.call("ka90001", body)
        out = []
        for r in rows(data, "thema_grp"):
            code = (r.get("thema_grp_cd") or "").strip()
            name = (r.get("thema_nm") or "").strip()
            if not code or not name:
                continue
            out.append({
                "thema_grp_cd": code[:20],
                "thema_nm": name[:60],
                "stk_num": to_int(r.get("stk_num")),
                "flu_rt": to_dec(r.get("flu_rt")),
                "dt_prft_rt": to_dec(r.get("dt_prft_rt")),
                "rising_stk_num": to_int(r.get("rising_stk_num")),
                "main_stk": (r.get("main_stk") or "").strip()[:200],
            })
        return self._store(key, out)  # type: ignore[return-value]

    def theme_members(self, thema_grp_cd: str, stex_tp: str = "1",
                      date_tp: str = "2") -> list[dict]:
        """ka90002 테마구성종목요청."""
        key = f"ka90002:{thema_grp_cd}:{stex_tp}:{date_tp}"
        hit = self._cached(key, THEME_CACHE_SEC)
        if hit is not None:
            return hit  # type: ignore[return-value]
        body = {"date_tp": date_tp, "thema_grp_cd": str(thema_grp_cd), "stex_tp": stex_tp}
        data, _ = self.rest.call("ka90002", body)
        out = []
        for r in rows(data, "thema_comp_stk"):
            code = norm_stk_cd(r.get("stk_cd"))
            if not code:
                continue
            out.append({
                "stk_cd": code,
                "stk_nm": (r.get("stk_nm") or "").strip()[:60],
                "cur_prc": abs(to_int(r.get("cur_prc"), 0) or 0),
                "flu_rt": to_dec(r.get("flu_rt")),
                "acc_trde_qty": to_int(r.get("acc_trde_qty")),
            })
        return self._store(key, out)  # type: ignore[return-value]

    # ------------------------------------------------------------------ #
    def quote(self, stk_cd: str) -> dict | None:
        """ka10001 주식기본정보 (현재가/시가/고저)."""
        key = f"ka10001:{stk_cd}"
        hit = self._cached(key, 20)
        if hit is not None:
            return hit  # type: ignore[return-value]
        data, _ = self.rest.call("ka10001", {"stk_cd": stk_cd})
        if not data or not data.get("stk_cd"):
            return None
        out = {
            "stk_cd": norm_stk_cd(data.get("stk_cd")),
            "stk_nm": (data.get("stk_nm") or "").strip(),
            "cur_prc": abs(to_int(data.get("cur_prc"), 0) or 0),
            "open_pric": abs(to_int(data.get("open_pric"), 0) or 0),
            "high_pric": abs(to_int(data.get("high_pric"), 0) or 0),
            "low_pric": abs(to_int(data.get("low_pric"), 0) or 0),
            "flu_rt": to_dec(data.get("flu_rt")),
            "trde_qty": to_int(data.get("trde_qty")),
        }
        return self._store(key, out)  # type: ignore[return-value]

    def daily_bars(self, stk_cd: str, base_dt: _dt.date | None = None,
                   store: bool = True) -> list[dict]:
        """ka10081 일봉 → price_daily 저장 후 과거순 리스트 반환."""
        key = f"ka10081:{stk_cd}"
        hit = self._cached(key, BARS_CACHE_SEC)
        if hit is not None:
            return hit  # type: ignore[return-value]
        base = (base_dt or today_kst()).strftime("%Y%m%d")
        data, _ = self.rest.call("ka10081", {"stk_cd": stk_cd, "base_dt": base, "upd_stkpc_tp": "1"})
        bars: list[dict] = []
        for r in rows(data, "stk_dt_pole_chart_qry"):
            dt = to_date(r.get("dt"))
            if not dt:
                continue
            bars.append({
                "dt": dt,
                "open_pric": abs(to_int(r.get("open_pric"), 0) or 0),
                "high_pric": abs(to_int(r.get("high_pric"), 0) or 0),
                "low_pric": abs(to_int(r.get("low_pric"), 0) or 0),
                "cur_prc": abs(to_int(r.get("cur_prc"), 0) or 0),
                "trde_qty": to_int(r.get("trde_qty")),
                "trde_prica": to_int(r.get("trde_prica")),
            })
        bars.sort(key=lambda b: b["dt"])
        if store and bars:
            self.db.upsert_price_daily(stk_cd, bars)
        return self._store(key, bars)  # type: ignore[return-value]

    def bars_from_db(self, stk_cd: str, limit: int = 30) -> list[dict]:
        return self.db.recent_bars(stk_cd, limit)
