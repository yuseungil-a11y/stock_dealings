"""계좌/예수금/보유종목 동기화 (ka00001, kt00001, kt00018) — 읽기 전용 TR."""
from __future__ import annotations

import logging

from ..db import Database
from ..kiwoom.parse import norm_stk_cd, rows, to_dec, to_int
from ..kiwoom.rest import KiwoomRest
from ..util import now_kst

log = logging.getLogger(__name__)


class AccountService:
    def __init__(self, db: Database, rest: KiwoomRest):
        self.db = db
        self.rest = rest
        # 마지막 kt00018 조회가 페이지 상한(max_pages)에 걸려 잘렸는지 (R-14).
        # 잘린 목록으로 replace_holdings 를 돌리면 못 받은 보유종목이 DELETE 된다.
        self.last_eval_truncated = False

    # ------------------------------------------------------------------ #
    def fetch_account_no(self) -> str | None:
        """ka00001 계좌번호조회."""
        data, _ = self.rest.call("ka00001", {})
        acct = data.get("acctNo") or data.get("acct_no")
        if isinstance(acct, list):
            acct = acct[0] if acct else None
        if isinstance(acct, dict):
            acct = acct.get("acctNo")
        return str(acct).strip() if acct else None

    def ensure_account(self, env: str) -> tuple[int, str]:
        """계좌번호를 조회해 account 테이블에 등록하고 (account_id, account_no) 반환."""
        acct_no = self.fetch_account_no()
        if not acct_no:
            raise RuntimeError("ka00001 응답에서 계좌번호를 찾지 못했습니다.")
        account_id = self.db.upsert_account(acct_no, env)
        return account_id, acct_no

    # ------------------------------------------------------------------ #
    def fetch_deposit(self, qry_tp: str = "2") -> dict:
        """kt00001 예수금상세현황."""
        data, _ = self.rest.call("kt00001", {"qry_tp": qry_tp})
        return {
            "entr": to_int(data.get("entr")),
            "d1_entra": to_int(data.get("d1_entra")),
            "d2_entra": to_int(data.get("d2_entra")),
            "ord_alow_amt": to_int(data.get("ord_alow_amt")),
            "pymn_alow_amt": to_int(data.get("pymn_alow_amt")),
        }

    def fetch_evaluation(self, qry_tp: str = "1", exchange: str = "KRX") -> tuple[dict, list[dict]]:
        """kt00018 계좌평가잔고내역 → (요약, 보유종목 리스트)."""
        pages, meta = self.rest.call_paged(
            "kt00018", {"qry_tp": qry_tp, "dmst_stex_tp": exchange}, with_meta=True)
        self.last_eval_truncated = bool(meta.get("truncated"))
        if self.last_eval_truncated:
            log.warning("kt00018 연속조회가 페이지 상한에서 잘렸습니다 - 보유종목 목록이 불완전합니다")
        head = pages[0] if pages else {}
        summary = {
            "tot_pur_amt": to_int(head.get("tot_pur_amt")),
            "tot_evlt_amt": to_int(head.get("tot_evlt_amt")),
            "tot_evlt_pl": to_int(head.get("tot_evlt_pl")),
            "tot_prft_rt": to_dec(head.get("tot_prft_rt")),
            "prsm_dpst_aset_amt": to_int(head.get("prsm_dpst_aset_amt")),
        }
        holdings: list[dict] = []
        for page in pages:
            for r in rows(page, "acnt_evlt_remn_indv_tot"):
                code = norm_stk_cd(r.get("stk_cd"))
                if not code:
                    continue
                holdings.append({
                    "stk_cd": code,
                    "stk_nm": (r.get("stk_nm") or "").strip()[:60],
                    "rmnd_qty": to_int(r.get("rmnd_qty"), 0) or 0,
                    "trde_able_qty": to_int(r.get("trde_able_qty")),
                    "pur_pric": to_int(r.get("pur_pric")),
                    "cur_prc": to_int(r.get("cur_prc")),
                    "pur_amt": to_int(r.get("pur_amt")),
                    "evlt_amt": to_int(r.get("evlt_amt")),
                    "evltv_prft": to_int(r.get("evltv_prft")),
                    "prft_rt": to_dec(r.get("prft_rt")),
                    "poss_rt": to_dec(r.get("poss_rt")),
                })
        return summary, holdings

    # ------------------------------------------------------------------ #
    def sync(self, account_id: int, exchange: str = "KRX") -> dict:
        """예수금 + 평가잔고를 DB 에 반영하고 요약을 반환."""
        deposit = self.fetch_deposit()
        summary, holdings = self.fetch_evaluation(exchange=exchange)
        data = {**deposit, **summary}
        snapshot_at = now_kst()
        self.db.insert_balance(account_id, snapshot_at, data)
        # R-14: 목록이 잘렸으면 '없어진 종목 삭제'를 건너뛴다(교체 대신 갱신만).
        full = not self.last_eval_truncated
        n = self.db.replace_holdings(account_id, holdings, delete_missing=full)
        if not full:
            msg = (f"보유종목 목록이 불완전해 교체를 건너뛰었습니다"
                   f"(받은 {n}종목만 갱신, 삭제 안 함)")
            log.warning("%s", msg)
            try:
                self.db.log_event("WARN", "sync", msg)
            except Exception:  # noqa: BLE001
                pass
            return {"snapshot_at": snapshot_at, "balance": data, "holdings": holdings,
                    "truncated": True}
        # B4/S-13: 더 이상 보유하지 않는 종목의 투입금·물타기 회차를 초기화
        try:
            reset = self.db.reset_stale_positions(
                account_id, [h["stk_cd"] for h in holdings], snapshot_at.date())
            if reset:
                log.info("보유하지 않는 종목 %d건의 position_state 초기화", reset)
        except Exception:  # noqa: BLE001 - 동기화 자체를 막지 않는다
            log.warning("position_state 초기화 실패", exc_info=True)
        log.info("계좌 동기화 완료: 예수금=%s 총평가=%s 보유 %d종목",
                 data.get("entr"), data.get("tot_evlt_amt"), n)
        return {"snapshot_at": snapshot_at, "balance": data, "holdings": holdings,
                "truncated": False}
