"""① 대시보드 — 예수금/평가/손익 요약 + 보유종목 표.

상장폐지(거래불가) 종목은 키움이 수익률을 0.00 으로 내려주므로 화면에서 그대로 보여주면
손실이 없는 것처럼 보인다. 웹 화면(web/lib/repo.php)과 **같은 기준**으로 표시를 보정한다:
현재가 0 이거나 종목명이 '(폐)' 로 시작하면 '상장폐지'로 표시하고
실수익률(= 평가손익 / 매입금액 × 100)을 쓴다.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from tkinter import ttk

from ..engine.context import DELISTED_PREFIX
from .widgets import fmt_money, fmt_rate

log = logging.getLogger(__name__)

COLUMNS = [
    ("stk_cd", "종목코드", 80),
    ("stk_nm", "종목명", 150),
    ("rmnd_qty", "보유수량", 80),
    ("pur_pric", "매입가", 90),
    ("cur_prc", "현재가", 90),
    ("evlt_amt", "평가금액", 110),
    ("evltv_prft", "평가손익", 110),
    ("prft_rt", "수익률", 80),
    ("state", "상태", 90),
]


def holding_is_delisted(row: dict) -> bool:
    """상장폐지·거래불가 보유종목 판정 (web/lib/repo.php 와 동일 기준)."""
    name = str(row.get("stk_nm") or "").strip()
    if name.startswith(DELISTED_PREFIX):
        return True
    cur = row.get("cur_prc")
    if cur is None:                 # 아직 미동기화 → 폐지로 단정하지 않는다
        return False
    try:
        return int(cur) <= 0
    except (TypeError, ValueError):
        return False


def holding_display_rate(row: dict):
    """화면 표시용 수익률. 상장폐지 종목은 실제 손익 기준(평가손익/매입금액)."""
    if not holding_is_delisted(row):
        return row.get("prft_rt")
    try:
        pur = Decimal(str(row.get("pur_amt") or 0))
        prft = Decimal(str(row.get("evltv_prft") or 0))
    except (InvalidOperation, TypeError, ValueError):
        return row.get("prft_rt")
    if pur == 0:
        return row.get("prft_rt")
    return prft / pur * 100

SUMMARY_FIELDS = [
    ("entr", "예수금"),
    ("ord_alow_amt", "주문가능금액"),
    ("tot_pur_amt", "총매입금액"),
    ("tot_evlt_amt", "총평가금액"),
    ("tot_evlt_pl", "총평가손익"),
    ("tot_prft_rt", "총수익률"),
    ("prsm_dpst_aset_amt", "추정예탁자산"),
]


class DashboardTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=8)
        self.app = app
        self._vars: dict[str, ttk.Label] = {}

        head = ttk.LabelFrame(self, text="계좌 요약", padding=8)
        head.pack(fill="x")
        for i, (key, label) in enumerate(SUMMARY_FIELDS):
            col = i % 4
            row = i // 4
            ttk.Label(head, text=label + " :").grid(row=row, column=col * 2, sticky="e", padx=(8, 4), pady=2)
            lab = ttk.Label(head, text="-", font=("맑은 고딕", 10, "bold"))
            lab.grid(row=row, column=col * 2 + 1, sticky="w", padx=(0, 16), pady=2)
            self._vars[key] = lab

        self.info = ttk.Label(self, text="계좌: (미확인)   갱신: -", foreground="#555555")
        self.info.pack(fill="x", pady=(6, 2))

        body = ttk.LabelFrame(self, text="보유종목", padding=4)
        body.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(body, columns=[c[0] for c in COLUMNS], show="headings", height=14)
        for key, title, width in COLUMNS:
            self.tree.heading(key, text=title,
                              command=lambda k=key: self._sort(k))
            self.tree.column(key, width=width, anchor="e" if key not in ("stk_cd", "stk_nm") else "w")
        ybar = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ybar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ybar.pack(side="right", fill="y")
        self.tree.tag_configure("up", foreground="#c62828")     # 한국 관례: 상승 빨강
        self.tree.tag_configure("down", foreground="#1565c0")   # 하락 파랑
        self.tree.tag_configure("delisted", foreground="#6d4c41", background="#efebe9")
        self._sort_key = None
        self._sort_desc = True

    # ------------------------------------------------------------------ #
    def _sort(self, key: str) -> None:
        self._sort_desc = not self._sort_desc if self._sort_key == key else True
        self._sort_key = key
        self.refresh()

    def refresh(self) -> None:
        db = self.app.db
        account_id = self.app.account_id()
        if not db or not account_id:
            return
        try:
            bal = db.latest_balance(account_id) or {}
            holdings = db.get_holdings(account_id)
            acct = db.get_account(account_id) or {}
        except Exception:  # noqa: BLE001
            log.debug("대시보드 조회 실패", exc_info=True)
            return

        for key, _ in SUMMARY_FIELDS:
            val = bal.get(key)
            text = fmt_rate(val) if key.endswith("_rt") else fmt_money(val)
            self._vars[key].configure(text=text)
            if key in ("tot_evlt_pl", "tot_prft_rt"):
                try:
                    v = float(val or 0)
                    self._vars[key].configure(foreground="#c62828" if v > 0 else
                                              ("#1565c0" if v < 0 else "#000000"))
                except (TypeError, ValueError):
                    pass

        from ..util import mask_account_no
        self.info.configure(
            text=f"계좌: {mask_account_no(acct.get('account_no'))} ({acct.get('env', '-')})   "
                 f"갱신: {bal.get('snapshot_at', '-')}   보유 {len(holdings)}종목")

        if self._sort_key:
            def keyfn(h):
                v = h.get(self._sort_key)
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return str(v or "")
            try:
                holdings = sorted(holdings, key=keyfn, reverse=self._sort_desc)
            except TypeError:
                pass

        self.tree.delete(*self.tree.get_children())
        for h in holdings:
            delisted = holding_is_delisted(h)
            rate = holding_display_rate(h)      # R-06: 폐지 종목은 실수익률로 보정
            try:
                rv = float(rate or 0)
            except (TypeError, ValueError):
                rv = 0.0
            if delisted:
                tag = "delisted"
            else:
                tag = "up" if rv > 0 else ("down" if rv < 0 else "")
            self.tree.insert("", "end", values=(
                h.get("stk_cd"), h.get("stk_nm"), fmt_money(h.get("rmnd_qty")),
                fmt_money(h.get("pur_pric")), fmt_money(h.get("cur_prc")),
                fmt_money(h.get("evlt_amt")), fmt_money(h.get("evltv_prft")),
                fmt_rate(rate), "상장폐지" if delisted else ""),
                tags=(tag,) if tag else ())
