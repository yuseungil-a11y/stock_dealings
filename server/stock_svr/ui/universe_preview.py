"""universe_filter — [대상 종목 미리보기] 팝업.

**폼에 입력된 값(저장 전)** 을 그대로 적용해 지금 어떤 종목이 매수 대상인지 보여준다.
DB(`stock_master`) 만 읽고 **키움 API 는 호출하지 않는다**. 조회는 백그라운드 스레드에서 하고
결과만 `after()` 로 UI 에 반영해 창이 멈추지 않게 한다.
"""
from __future__ import annotations

import logging
import threading
import tkinter as tk
from tkinter import ttk

from ..algo.universe_filter import (
    COMBINED_KEY,
    EOK,
    MARKET_LABEL,
    SCOPE_COMBINED,
    Universe,
    UniverseOptions,
    load_universe,
)

log = logging.getLogger(__name__)

# 표에 그리는 최대 행 수(수천 종목을 한 번에 그리면 창이 느려진다)
MAX_ROWS = 400


# ====================================================================== #
# 순수 계산 (테스트 대상)
# ====================================================================== #
def summary_lines(uni: Universe, opts: UniverseOptions) -> list[str]:
    """요약 문구(대상 종목 수·시총 컷오프·마스터 갱신 시각)."""
    out: list[str] = []
    per_market = ", ".join(
        f"{MARKET_LABEL.get(m, m)} {uni.passed_by_market.get(m, 0)}종목"
        for m in opts.markets) or "(대상 시장 없음)"
    out.append(f"대상 종목 : 총 {uni.passed_total}종목  ({per_market})")
    if opts.rank_scope == SCOPE_COMBINED:
        cap = uni.cutoff_by_scope.get(COMBINED_KEY)
        out.append(f"시총 컷오프 : {opts.combined_label} "
                   f"{'-' if cap is None else format(cap // EOK, ',') + '억원'}")
    else:
        parts = []
        for m in opts.markets:
            cap = uni.cutoff_by_scope.get(m)
            parts.append(f"{MARKET_LABEL.get(m, m)} "
                         f"{'-' if cap is None else format(cap // EOK, ',') + '억원'}")
        out.append("시총 컷오프 : " + ("  /  ".join(parts) or "-"))
    stamp = uni.updated_at.strftime("%Y-%m-%d %H:%M") if uni.updated_at else "확인 불가"
    out.append(f"종목마스터 최신 갱신 : {stamp}  (코스피·코스닥·ETF {uni.source_rows:,}행)")
    return out


def preview_rows(uni: Universe, opts: UniverseOptions, only_passed: bool = True,
                 search: str = "", limit: int = MAX_ROWS) -> list[tuple]:
    """표에 그릴 행: (순위, 코드, 종목명, 시장, 시총(억), 전일종가, 통과/사유)."""
    needle = (search or "").strip().lower()
    # 통과 종목을 순위순으로 먼저, 그 다음 제외 종목
    ordered = list(uni.ranked)
    if not only_passed:
        ordered += [e for e in uni.entries.values() if e.excluded]
    rows: list[tuple] = []
    for e in ordered:
        if only_passed and not e.passed:
            continue
        if needle and needle not in e.stk_cd.lower() and needle not in e.stk_nm.lower():
            continue
        rows.append((
            "-" if e.rank is None else str(e.rank),
            e.stk_cd,
            e.stk_nm,
            e.market_label,
            f"{e.cap_eok:,}",
            f"{e.last_price:,}",
            "통과" if e.passed else e.reason,
        ))
        if len(rows) >= limit:
            break
    return rows


# ====================================================================== #
class UniversePreviewDialog(tk.Toplevel):
    """대상 종목 미리보기 창."""

    def __init__(self, master, db, opts: UniverseOptions, now=None):
        super().__init__(master)
        self.db = db
        self.opts = opts
        self._now = now
        self.uni: Universe | None = None
        self.title("대상 종목 미리보기 (universe_filter)")
        self.geometry("880x560")
        self.transient(master)

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)

        cond = (f"{opts.markets_text} · "
                f"{'합산' if opts.rank_scope == SCOPE_COMBINED else '시장별'} 시총 상위 "
                f"{opts.top_n} · 최소 주가 {opts.min_price:,}원"
                + (f" · 최대 주가 {opts.max_price:,}원" if opts.max_price > 0 else "")
                + (f" · 최소 시총 {opts.min_market_cap_eok:,}억원"
                   if opts.min_market_cap_eok > 0 else ""))
        ttk.Label(frm, text=cond, font=("맑은 고딕", 10, "bold"),
                  wraplength=840, justify="left").pack(anchor="w")

        self.summary_var = tk.StringVar(value="종목마스터를 읽는 중…")
        ttk.Label(frm, textvariable=self.summary_var, foreground="#1565c0",
                  justify="left").pack(anchor="w", pady=(4, 6))

        bar = ttk.Frame(frm)
        bar.pack(fill="x")
        self.only_passed = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="통과 종목만 보기", variable=self.only_passed,
                        command=self._render).pack(side="left")
        ttk.Label(bar, text="검색").pack(side="left", padx=(12, 4))
        self.search_var = tk.StringVar()
        ent = ttk.Entry(bar, textvariable=self.search_var, width=20)
        ent.pack(side="left")
        self.search_var.trace_add("write", lambda *_: self._render())
        self.count_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.count_var, foreground="#777777").pack(side="left", padx=10)

        cols = ("rank", "code", "name", "market", "cap", "price", "result")
        heads = ("순위", "코드", "종목명", "시장", "시총(억)", "전일종가", "통과/제외 사유")
        widths = (55, 70, 190, 60, 100, 90, 280)
        wrap = ttk.Frame(frm)
        wrap.pack(fill="both", expand=True, pady=(6, 0))
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", height=18)
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="e" if c in ("rank", "cap", "price") else "w")
        ybar = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ybar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ybar.pack(side="right", fill="y")

        ttk.Button(frm, text="닫기", command=self.destroy).pack(anchor="e", pady=(8, 0))
        self.bind("<Escape>", lambda e: self.destroy())
        self._start_load()

    # ------------------------------------------------------------------ #
    def _start_load(self) -> None:
        def work():
            try:
                uni, err = load_universe(self.db, self.opts, self._now)
            except Exception as exc:  # noqa: BLE001
                log.exception("대상 종목 미리보기 조회 실패")
                uni, err = None, f"조회 실패: {type(exc).__name__}: {exc}"
            try:
                self.after(0, lambda: self._on_loaded(uni, err))
            except tk.TclError:
                pass        # 창이 이미 닫힘

        threading.Thread(target=work, name="universe-preview", daemon=True).start()

    def _on_loaded(self, uni: Universe | None, err: str) -> None:
        if not self.winfo_exists():
            return
        self.uni = uni
        if uni is None:
            self.summary_var.set(f"⚠ {err}\n(이 상태에서는 신규 매수가 차단됩니다)")
            return
        self.summary_var.set("\n".join(summary_lines(uni, self.opts)))
        self._render()

    def _render(self) -> None:
        if self.uni is None:
            return
        rows = preview_rows(self.uni, self.opts, only_passed=self.only_passed.get(),
                            search=self.search_var.get(), limit=MAX_ROWS)
        self.tree.delete(*self.tree.get_children())
        for r in rows:
            self.tree.insert("", "end", values=r)
        more = " (상한까지만 표시)" if len(rows) >= MAX_ROWS else ""
        self.count_var.set(f"{len(rows)}행 표시{more}")
