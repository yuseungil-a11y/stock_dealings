"""장마감 후 정리 작업 및 로그 보관기간 관리.

* kt00015 위탁종합거래내역 → trade_ledger
* ka10170 당일매매일지 → daily_trade_summary
* holding → holding_snapshot
* event_log / api_call_log / 로그 파일 7일 초과분 삭제
"""
from __future__ import annotations

import datetime as _dt
import logging

from ..db import Database
from ..kiwoom.parse import norm_stk_cd, rows, to_date, to_dec, to_int
from ..kiwoom.rest import KiwoomRest
from ..logging_setup import purge_old_logs
from ..util import today_kst

log = logging.getLogger(__name__)

# 장마감 정리 작업 실패 시 재시도 간격/일일 최대 시도 횟수 (B1)
EOD_RETRY_SEC = 600
EOD_MAX_ATTEMPTS = 6


class HousekeepingService:
    def __init__(self, db: Database, rest: KiwoomRest | None, account_id: int | None = None):
        self.db = db
        self.rest = rest
        self.account_id = account_id
        self._last_eod_date: _dt.date | None = None
        self._last_purge_date: _dt.date | None = None
        self._eod_attempt_date: _dt.date | None = None
        self._eod_attempts = 0
        self._eod_next_try: _dt.datetime | None = None

    # ------------------------------------------------------------------ #
    def sync_trade_ledger(self, days: int = 5, exchange: str = "KRX") -> int:
        """kt00015 위탁종합거래내역 (최근 며칠)."""
        if not self.rest or not self.account_id:
            return 0
        end = today_kst()
        start = end - _dt.timedelta(days=max(0, days))
        body = {
            "strt_dt": start.strftime("%Y%m%d"), "end_dt": end.strftime("%Y%m%d"),
            "tp": "0", "stk_cd": "", "crnc_cd": "", "gds_tp": "0",
            "frgn_stex_code": "", "dmst_stex_tp": exchange, "qry_sort_tp": "1",
        }
        pages = self.rest.call_paged("kt00015", body, max_pages=10)
        items: list[dict] = []
        for page in pages:
            for r in rows(page, "trst_ovrl_trde_prps_array"):
                dt = to_date(r.get("trde_dt"))
                trde_no = (r.get("trde_no") or "").strip()
                if not dt or not trde_no:
                    continue
                tax = (to_int(r.get("trde_agri_tax"), 0) or 0) + (to_int(r.get("incm_resi_tax"), 0) or 0)
                items.append({
                    "trde_dt": dt,
                    "trde_no": trde_no[:20],
                    "trde_kind_nm": (r.get("trde_kind_nm") or "")[:30] or None,
                    "rmrk_nm": (r.get("rmrk_nm") or "")[:80] or None,
                    "stk_cd": norm_stk_cd(r.get("stk_cd")) or None,
                    "stk_nm": (r.get("stk_nm") or "")[:60] or None,
                    "trde_qty": to_int(r.get("trde_qty_jwa_cnt")),
                    "trde_unit": to_int(r.get("trde_unit")),
                    "trde_amt": to_int(r.get("trde_amt")),
                    "cmsn": to_int(r.get("cmsn")),
                    "tax": tax,
                    "exct_amt": to_int(r.get("exct_amt")),
                    "entra_remn": to_int(r.get("entra_remn")),
                    "proc_tm": (r.get("proc_tm") or "")[:20] or None,
                })
        n = self.db.upsert_trade_ledger(self.account_id, items)
        log.info("거래내역(kt00015) %d건 반영", len(items))
        return n

    def sync_daily_diary(self, base_dt: _dt.date | None = None) -> int:
        """ka10170 당일매매일지 → daily_trade_summary."""
        if not self.rest or not self.account_id:
            return 0
        base = base_dt or today_kst()
        body = {"base_dt": base.strftime("%Y%m%d"), "ottks_tp": "1", "ch_crd_tp": "0"}
        pages = self.rest.call_paged("ka10170", body, max_pages=5)
        items: list[dict] = []
        for page in pages:
            for r in rows(page, "tdy_trde_diary"):
                code = norm_stk_cd(r.get("stk_cd"))
                if not code:
                    continue
                items.append({
                    "stk_cd": code,
                    "stk_nm": (r.get("stk_nm") or "")[:60] or None,
                    "buy_qty": to_int(r.get("buy_qty")),
                    "buy_avg_pric": to_int(r.get("buy_avg_pric")),
                    "buy_amt": to_int(r.get("buy_amt")),
                    "sell_qty": to_int(r.get("sell_qty")),
                    "sell_avg_pric": to_int(r.get("sel_avg_pric")),
                    "sell_amt": to_int(r.get("sell_amt")),
                    "cmsn_tax": to_int(r.get("cmsn_alm_tax")),
                    "pl_amt": to_int(r.get("pl_amt")),
                    "prft_rt": to_dec(r.get("prft_rt")),
                })
        n = self.db.upsert_daily_summary(self.account_id, base, items)
        log.info("매매일지(ka10170) %d건 반영", len(items))
        return n

    def snapshot_holdings(self, base_dt: _dt.date | None = None) -> int:
        if not self.account_id:
            return 0
        return self.db.snapshot_holdings(self.account_id, base_dt or today_kst())

    # ------------------------------------------------------------------ #
    def run_end_of_day(self, force: bool = False) -> bool:
        """장마감 후 1회 실행. 실패하면 백오프 후 재시도하되 하루 최대 N회 (B1)."""
        today = today_kst()
        if not force and self._last_eod_date == today:
            return False
        if self._eod_attempt_date != today:
            self._eod_attempt_date = today
            self._eod_attempts = 0
            self._eod_next_try = None
        now = _dt.datetime.now()
        if not force:
            if self._eod_next_try and now < self._eod_next_try:
                return False
            if self._eod_attempts >= EOD_MAX_ATTEMPTS:
                return False
        # 시도 사실을 먼저 마킹해 실패 시 폭주를 막는다
        self._eod_attempts += 1
        self._eod_next_try = now + _dt.timedelta(seconds=EOD_RETRY_SEC)
        try:
            self.sync_trade_ledger()
            self.sync_daily_diary(today)
            self.snapshot_holdings(today)
            self._last_eod_date = today
            log.info("장마감 정리 작업 완료 (%s, %d회차)", today, self._eod_attempts)
            return True
        except Exception:  # noqa: BLE001 - 엔진을 죽이지 않는다
            log.exception("장마감 정리 작업 실패 (%d/%d회차, %d초 후 재시도)",
                          self._eod_attempts, EOD_MAX_ATTEMPTS, EOD_RETRY_SEC)
            return False

    def run_purge(self, retention_days: int, log_dir=None, force: bool = False) -> dict:
        """보관기간 초과 로그 삭제 (기동 시 1회 + 매일 1회).

        * 단기 로그(`event_log`/`api_call_log`/`screening_result`) → `purge_old` (기본 7일)
        * 영구 보관본(`event_archive`/`api_error_log`) → `purge_archives` (기본 365일)

        주문·체결·신호·주문이벤트 등 **거래 원장은 어느 쪽에서도 지우지 않는다.**
        """
        today = today_kst()
        if not force and self._last_purge_date == today:
            return {}
        self._last_purge_date = today
        result = self.db.purge_old(retention_days)
        try:
            result.update(self.db.purge_archives())
        except Exception:  # noqa: BLE001 - 정리 실패가 하우스키핑 전체를 멈추지 않는다
            log.warning("아카이브(event_archive/api_error_log) 정리 실패", exc_info=True)
        if log_dir is not None:
            result["log_files"] = purge_old_logs(log_dir, retention_days)
        log.info("보관기간(%d일) 초과 로그 정리: %s", retention_days, result)
        return result
