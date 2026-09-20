"""미체결/체결 동기화 (ka10075, ka10076) + WebSocket 주문체결(00)/잔고(04) 처리 — 읽기 전용 TR."""
from __future__ import annotations

import hashlib
import logging

from ..db import Database
from ..kiwoom.parse import hhmmss_to_dt, norm_stk_cd, rows, side_from_code, to_int
from ..kiwoom.rest import KiwoomRest
from ..util import now_kst, today_kst

log = logging.getLogger(__name__)

# 키움 주문상태 텍스트 → orders.status
_STATUS_MAP = {
    "접수": "ACCEPTED",
    "확인": "ACCEPTED",
    "체결": "FILLED",
    "부분체결": "PARTIAL",
    "취소": "CANCELED",
    "취소확인": "CANCELED",
    "거부": "REJECTED",
}


def execution_key(ord_no: str, ord_tm, cntr_pric: int, cntr_qty: int) -> str:
    """체결번호가 없는 REST 응답용 **결정적 체결키** (B2).

    같은 체결을 여러 번 조회해도 동일한 키가 나오므로 UNIQUE(account,ord_no,cntr_no)
    제약에 걸려 중복 저장되지 않는다.
    """
    raw = f"{ord_no}|{str(ord_tm or '').strip()}|{int(cntr_pric)}|{int(cntr_qty)}"
    return "R" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:15]


def map_status(text: str | None, oso_qty: int | None = None, filled: int | None = None,
               ord_qty: int | None = None) -> str:
    s = (text or "").strip()
    for key, val in _STATUS_MAP.items():
        if key in s:
            if val == "FILLED" and oso_qty:
                return "PARTIAL"
            return val
    if ord_qty and filled:
        if filled >= ord_qty:
            return "FILLED"
        if filled > 0:
            return "PARTIAL"
    return "ACCEPTED"


class OrderSyncService:
    def __init__(self, db: Database, rest: KiwoomRest, account_id: int):
        self.db = db
        self.rest = rest
        self.account_id = account_id

    # -- REST ---------------------------------------------------------- #
    def fetch_open_orders(self, exchange_tp: str = "0") -> list[dict]:
        """ka10075 미체결 목록(취소 대상)을 간단한 형태로 반환 (S-15)."""
        body = {"all_stk_tp": "0", "trde_tp": "0", "stk_cd": "", "stex_tp": exchange_tp}
        pages = self.rest.call_paged("ka10075", body, max_pages=5)
        out: list[dict] = []
        for page in pages:
            for r in rows(page, "oso"):
                ord_no = (r.get("ord_no") or "").strip()
                oso_qty = to_int(r.get("oso_qty"), 0) or 0
                if not ord_no or oso_qty <= 0:
                    continue
                out.append({
                    "ord_no": ord_no,
                    "stk_cd": norm_stk_cd(r.get("stk_cd")),
                    # R-04: UI 취소 대상 목록에 종목명·주문수량·방향까지 보여준다
                    "stk_nm": (r.get("stk_nm") or "").strip()[:60],
                    "ord_qty": to_int(r.get("ord_qty"), 0) or 0,
                    "side": side_from_code(r.get("io_tp_nm")) or "",
                    "oso_qty": oso_qty,
                    "exchange": (r.get("stex_tp_txt") or "KRX")[:5],
                })
        return out

    def sync_open_orders(self, exchange_tp: str = "0") -> int:
        """ka10075 미체결요청 → orders upsert."""
        body = {"all_stk_tp": "0", "trde_tp": "0", "stk_cd": "", "stex_tp": exchange_tp}
        pages = self.rest.call_paged("ka10075", body, max_pages=5)
        n = 0
        for page in pages:
            for r in rows(page, "oso"):
                ord_no = (r.get("ord_no") or "").strip()
                if not ord_no:
                    continue
                ord_qty = to_int(r.get("ord_qty"), 0) or 0
                oso_qty = to_int(r.get("oso_qty"), 0) or 0
                filled = max(0, ord_qty - oso_qty)
                self.db.upsert_order_by_ordno(
                    self.account_id, ord_no,
                    orig_ord_no=(r.get("orig_ord_no") or "").strip() or None,
                    side=side_from_code(r.get("io_tp_nm")) or "BUY",
                    stk_cd=norm_stk_cd(r.get("stk_cd")),
                    stk_nm=(r.get("stk_nm") or "").strip()[:60] or None,
                    trde_tp=(r.get("trde_tp") or "0")[:3],
                    ord_qty=ord_qty,
                    ord_uv=to_int(r.get("ord_pric")),
                    status=map_status(r.get("ord_stt"), oso_qty, filled, ord_qty),
                    filled_qty=filled,
                    dmst_stex_tp=(r.get("stex_tp_txt") or "KRX")[:5],
                )
                n += 1
        if n:
            log.info("미체결 %d건 동기화", n)
        return n

    def sync_executions(self, exchange_tp: str = "0") -> int:
        """ka10076 체결요청 → executions upsert + orders 상태 반영."""
        body = {"stk_cd": "", "qry_tp": "0", "sell_tp": "0", "ord_no": "", "stex_tp": exchange_tp}
        pages = self.rest.call_paged("ka10076", body, max_pages=5)
        n = 0
        today = today_kst()
        for page in pages:
            for r in rows(page, "cntr"):
                ord_no = (r.get("ord_no") or "").strip()
                cntr_qty = to_int(r.get("cntr_qty"), 0) or 0
                cntr_pric = to_int(r.get("cntr_pric"), 0) or 0
                if not ord_no or cntr_qty <= 0:
                    continue
                stk_cd = norm_stk_cd(r.get("stk_cd"))
                side = side_from_code(r.get("io_tp_nm")) or "BUY"
                executed_at = hhmmss_to_dt(r.get("ord_tm"), today) or now_kst()
                ord_qty = to_int(r.get("ord_qty"), 0) or 0
                oso_qty = to_int(r.get("oso_qty"), 0) or 0
                filled = max(0, ord_qty - oso_qty) or cntr_qty      # B8: 산식 통일
                # B2: 체결번호가 없는 REST 응답은 결정적 키를 만들어 WS(909) 건과
                #     중복 저장되지 않게 한다. WS 가 정본, REST 는 보정 역할.
                cntr_no = execution_key(ord_no, r.get("ord_tm"), cntr_pric, cntr_qty)
                self.db.upsert_execution(
                    self.account_id, ord_no, cntr_no, stk_cd,
                    (r.get("stk_nm") or "").strip()[:60] or None, side, cntr_qty, cntr_pric,
                    executed_at,
                    cmsn=to_int(r.get("tdy_trde_cmsn")), tax=to_int(r.get("tdy_trde_tax")),
                    source="REST", only_if_absent=True,
                )
                self.db.upsert_order_by_ordno(
                    self.account_id, ord_no,
                    side=side, stk_cd=stk_cd,
                    stk_nm=(r.get("stk_nm") or "").strip()[:60] or None,
                    trde_tp=(r.get("trde_tp") or "0")[:3],
                    ord_qty=ord_qty or cntr_qty,
                    ord_uv=to_int(r.get("ord_pric")),
                    status=map_status(r.get("ord_stt"), oso_qty, filled, ord_qty),
                    filled_qty=filled,
                    avg_fill_pric=cntr_pric,
                )
                n += 1
        if n:
            log.info("체결 %d건 동기화", n)
        return n

    # -- WebSocket ----------------------------------------------------- #
    def on_order_exec(self, values: dict) -> None:
        """WS `00` 주문체결 실시간."""
        ord_no = str(values.get("9203", "")).strip()
        if not ord_no:
            return
        stk_cd = norm_stk_cd(values.get("9001"))
        stk_nm = (str(values.get("302", "")) or "").strip()[:60] or None
        side = side_from_code(values.get("907")) or "BUY"
        ord_qty = to_int(values.get("900"), 0) or 0
        oso_qty = to_int(values.get("902"), 0) or 0
        cntr_qty = to_int(values.get("911"), 0) or 0
        cntr_pric = abs(to_int(values.get("910"), 0) or 0)
        cntr_no = str(values.get("909", "")).strip()
        status = map_status(values.get("913"), oso_qty, cntr_qty, ord_qty)

        self.db.upsert_order_by_ordno(
            self.account_id, ord_no,
            orig_ord_no=str(values.get("904", "")).strip() or None,
            side=side, stk_cd=stk_cd, stk_nm=stk_nm,
            trde_tp=str(values.get("906", "0"))[:3],
            ord_qty=ord_qty, ord_uv=abs(to_int(values.get("901"), 0) or 0) or None,
            status=status,
            filled_qty=max(0, ord_qty - oso_qty) if ord_qty else cntr_qty,
            avg_fill_pric=cntr_pric or None,
        )
        if cntr_qty > 0 and cntr_pric > 0:
            executed_at = hhmmss_to_dt(values.get("908"), today_kst()) or now_kst()
            self.db.upsert_execution(
                self.account_id, ord_no, cntr_no, stk_cd, stk_nm, side,
                cntr_qty, cntr_pric, executed_at,
                cmsn=to_int(values.get("938")), tax=to_int(values.get("939")), source="WS",
            )
            if side == "SELL":
                # B4: 매도 체결분만큼 누적 투입금 차감
                try:
                    self.db.reduce_position_invest(self.account_id, stk_cd, cntr_qty * cntr_pric)
                except Exception:  # noqa: BLE001
                    log.debug("position_state 차감 실패", exc_info=True)
            log.info("체결 수신: %s %s %s주 @%s", stk_cd, side, cntr_qty, cntr_pric)

    def on_balance(self, values: dict) -> None:
        """WS `04` 잔고 실시간 → holding 즉시 반영."""
        stk_cd = norm_stk_cd(values.get("9001"))
        if not stk_cd:
            return
        qty = to_int(values.get("930"), 0) or 0
        if qty <= 0:
            self.db.execute("DELETE FROM holding WHERE account_id=%s AND stk_cd=%s",
                            (self.account_id, stk_cd))
            return
        self.db.execute(
            "INSERT INTO holding (account_id, stk_cd, stk_nm, rmnd_qty, trde_able_qty, pur_pric, cur_prc) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE rmnd_qty=VALUES(rmnd_qty), trde_able_qty=VALUES(trde_able_qty), "
            "pur_pric=VALUES(pur_pric), cur_prc=VALUES(cur_prc), stk_nm=VALUES(stk_nm)",
            (self.account_id, stk_cd, (str(values.get("302", "")) or stk_cd).strip()[:60], qty,
             to_int(values.get("933")), abs(to_int(values.get("931"), 0) or 0),
             abs(to_int(values.get("10"), 0) or 0)),
        )
