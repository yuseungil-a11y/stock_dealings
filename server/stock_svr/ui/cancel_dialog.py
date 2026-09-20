"""긴급 미체결 취소 대상 선택 대화상자 (R-04).

ka10075 는 **계좌 전체** 미체결을 돌려준다 — 사용자가 HTS 로 직접 낸 주문도 포함된다.
그래서 취소 전에 대상 목록(종목·수량·주문번호·우리 알고리즘 주문 여부)을 먼저 보여주고,
**기본 선택은 우리 알고리즘이 낸 주문만** 으로 둔다. 실계좌 확인(REAL 입력)은 그대로 유지한다.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

WARN_TEXT = ("⚠ 이 목록은 **계좌 전체** 미체결 주문입니다.\n"
             "   HTS 등에서 직접 내신 주문도 포함될 수 있으니 선택을 확인하세요.\n"
             "   (기본 선택: 이 프로그램의 알고리즘이 낸 주문만)")


def default_selection(targets: list[dict]) -> list[str]:
    """기본 선택 = 우리 알고리즘이 낸 주문의 주문번호."""
    return [str(t.get("ord_no")) for t in targets if t.get("is_ours")]


def describe_target(t: dict) -> tuple[str, str, str, str, str]:
    """표 한 줄 (주문번호, 종목코드, 종목명, 미체결수량, 출처)."""
    origin = f"알고리즘({t.get('algo_code')})" if t.get("is_ours") else "외부/수동 주문"
    return (str(t.get("ord_no") or ""), str(t.get("stk_cd") or ""),
            str(t.get("stk_nm") or ""), f"{int(t.get('oso_qty') or 0):,}", origin)


class CancelTargetsDialog(tk.Toplevel):
    """취소 대상 선택 창. `result` 에 선택된 주문번호 목록(취소 시 None)."""

    def __init__(self, master, targets: list[dict]):
        super().__init__(master)
        self.targets = list(targets)
        self.result: list[str] | None = None
        self.title("긴급 미체결 취소 - 대상 선택")
        self.resizable(True, False)
        self.transient(master)

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="미체결 주문 취소 대상", font=("맑은 고딕", 12, "bold")).pack(anchor="w")
        ttk.Label(frm, text=WARN_TEXT, foreground="#c62828", justify="left").pack(
            anchor="w", pady=(6, 8))

        cols = [("ord_no", "주문번호", 110), ("stk_cd", "종목코드", 80),
                ("stk_nm", "종목명", 150), ("oso_qty", "미체결", 80),
                ("origin", "출처", 160)]
        self.tree = ttk.Treeview(frm, columns=[c[0] for c in cols], show="headings",
                                 height=min(12, max(3, len(self.targets))),
                                 selectmode="extended")
        for key, title, width in cols:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width,
                             anchor="e" if key == "oso_qty" else "w")
        self.tree.pack(fill="both", expand=True)
        self.tree.tag_configure("ours", foreground="#1565c0")
        self.tree.tag_configure("external", foreground="#b26a00")

        preselect = set(default_selection(self.targets))
        for t in self.targets:
            iid = str(t.get("ord_no"))
            self.tree.insert("", "end", iid=iid, values=describe_target(t),
                             tags=("ours" if t.get("is_ours") else "external",))
        for iid in preselect:
            try:
                self.tree.selection_add(iid)
            except tk.TclError:
                pass

        hint = ttk.Frame(frm)
        hint.pack(fill="x", pady=(8, 0))
        ttk.Button(hint, text="우리 알고리즘 주문만 선택",
                   command=self._select_ours).pack(side="left")
        ttk.Button(hint, text="전체 선택", command=self._select_all).pack(side="left", padx=6)
        self.count_var = tk.StringVar()
        ttk.Label(hint, textvariable=self.count_var).pack(side="left", padx=12)

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="선택한 주문 취소", command=self._ok).pack(side="right")
        ttk.Button(btns, text="닫기", command=self._cancel).pack(side="right", padx=6)

        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_count())
        self._update_count()
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda e: self._cancel())
        self.grab_set()

    # ------------------------------------------------------------------ #
    def _select_ours(self) -> None:
        self.tree.selection_set(default_selection(self.targets))
        self._update_count()

    def _select_all(self) -> None:
        self.tree.selection_set([str(t.get("ord_no")) for t in self.targets])
        self._update_count()

    def _update_count(self) -> None:
        sel = set(self.tree.selection())
        ext = sum(1 for t in self.targets
                  if str(t.get("ord_no")) in sel and not t.get("is_ours"))
        self.count_var.set(f"선택 {len(sel)}건"
                           + (f"  (외부/수동 주문 {ext}건 포함 ⚠)" if ext else ""))

    def _ok(self) -> None:
        self.result = list(self.tree.selection())
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()
