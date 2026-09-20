"""공용 위젯: 상태 아이콘(색 원 + 라벨), 툴팁."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

COLORS = {
    "ok": "#22a36b",       # 녹색 = 정상
    "warn": "#e0a300",     # 황색 = 주의/재연결중
    "error": "#d64545",    # 적색 = 오류
    "unknown": "#9aa0a6",  # 회색 = 미확인
    "alert": "#c62828",    # 실전 주문 ON 경고
}


class Tooltip:
    """마우스 오버 시 최근 메시지 표시."""

    def __init__(self, widget: tk.Widget, text: str = ""):
        self.widget = widget
        self.text = text
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def set(self, text: str) -> None:
        self.text = text or ""

    def _show(self, _evt=None) -> None:
        if self._tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        tk.Label(tip, text=self.text, justify="left", background="#ffffe0",
                 relief="solid", borderwidth=1, font=("맑은 고딕", 9), padx=6, pady=3).pack()
        self._tip = tip

    def _hide(self, _evt=None) -> None:
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


class StatusLight(ttk.Frame):
    """색 원 + 라벨. status 는 ok/warn/error/unknown/alert."""

    SIZE = 14

    def __init__(self, master, label: str, **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, width=self.SIZE, height=self.SIZE,
                                highlightthickness=0, bd=0)
        self._circle = self.canvas.create_oval(2, 2, self.SIZE - 2, self.SIZE - 2,
                                               fill=COLORS["unknown"], outline="#666666")
        self.canvas.pack(side="left", padx=(0, 4))
        self.label_text = label
        self.var = tk.StringVar(value=label)
        self.label = ttk.Label(self, textvariable=self.var)
        self.label.pack(side="left")
        self.tooltip = Tooltip(self, f"{label}: 미확인")

    def update_state(self, status: str, text: str | None = None, message: str = "") -> None:
        color = COLORS.get(status, COLORS["unknown"])
        self.canvas.itemconfig(self._circle, fill=color)
        self.var.set(text if text is not None else self.label_text)
        self.tooltip.set(f"{self.label_text}: {text or status}" + (f"\n{message}" if message else ""))


class ScrollingText(ttk.Frame):
    """자동 스크롤 텍스트 영역."""

    def __init__(self, master, height: int = 20, **kw):
        super().__init__(master, **kw)
        self.text = tk.Text(self, height=height, wrap="none", state="disabled",
                            font=("Consolas", 9))
        ybar = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        xbar = ttk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.text.tag_configure("DEBUG", foreground="#777777")
        self.text.tag_configure("INFO", foreground="#111111")
        self.text.tag_configure("WARN", foreground="#b26a00")
        self.text.tag_configure("WARNING", foreground="#b26a00")
        self.text.tag_configure("ERROR", foreground="#c62828")
        self.autoscroll = True
        self.max_lines = 2000

    def append(self, line: str, level: str = "INFO") -> None:
        self.text.configure(state="normal")
        self.text.insert("end", line + "\n", level)
        count = int(self.text.index("end-1c").split(".")[0])
        if count > self.max_lines:
            self.text.delete("1.0", f"{count - self.max_lines}.0")
        self.text.configure(state="disabled")
        if self.autoscroll:
            self.text.see("end")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")


def fmt_money(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "-"


def fmt_rate(value) -> str:
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "-"
