"""② 주요 기록 — 이벤트 로그 실시간 출력(레벨 필터 + 자동 스크롤)."""
from __future__ import annotations

import queue
import tkinter as tk
from tkinter import ttk

from ..logging_setup import ui_queue
from .widgets import ScrollingText

LEVELS = ["DEBUG", "INFO", "WARN", "ERROR"]
_ORDER = {"DEBUG": 0, "INFO": 1, "WARN": 2, "WARNING": 2, "ERROR": 3, "CRITICAL": 3}
MAX_DRAIN = 200


class LogTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=6)
        self.app = app

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 4))
        ttk.Label(bar, text="레벨 필터:").pack(side="left")
        self.level_var = tk.StringVar(value="INFO")
        combo = ttk.Combobox(bar, textvariable=self.level_var, values=LEVELS,
                             state="readonly", width=8)
        combo.pack(side="left", padx=(4, 12))
        self.autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="자동 스크롤", variable=self.autoscroll_var,
                        command=self._toggle_scroll).pack(side="left")
        ttk.Button(bar, text="지우기", command=self._clear).pack(side="left", padx=8)
        ttk.Button(bar, text="DB 이벤트 다시 읽기", command=self.load_from_db).pack(side="left")
        self.count_var = tk.StringVar(value="0건")
        ttk.Label(bar, textvariable=self.count_var).pack(side="right")

        self.view = ScrollingText(self, height=22)
        self.view.pack(fill="both", expand=True)
        self._count = 0

    # ------------------------------------------------------------------ #
    def _toggle_scroll(self) -> None:
        self.view.autoscroll = bool(self.autoscroll_var.get())

    def _clear(self) -> None:
        self.view.clear()
        self._count = 0
        self.count_var.set("0건")

    def _passes(self, level: str) -> bool:
        return _ORDER.get(level.upper(), 1) >= _ORDER.get(self.level_var.get(), 1)

    def add(self, ts, level: str, category: str, message: str) -> None:
        if not self._passes(level):
            return
        stamp = ts.strftime("%H:%M:%S") if hasattr(ts, "strftime") else str(ts)
        norm = "WARN" if level.upper().startswith("WARN") else level.upper()
        self.view.append(f"{stamp} [{norm:<5}] {category:<8} {message}", norm)
        self._count += 1
        self.count_var.set(f"{self._count}건")

    def drain_queue(self) -> None:
        """UI 스레드에서 로그 큐를 비운다(블로킹 금지)."""
        for _ in range(MAX_DRAIN):
            try:
                item = ui_queue.get_nowait()
            except queue.Empty:
                return
            self.add(item["ts"], item["level"], item["category"], item["message"])

    def load_from_db(self) -> None:
        db = self.app.db
        if not db:
            return
        try:
            rows = db.recent_events(limit=300, min_level=self.level_var.get())
        except Exception:  # noqa: BLE001
            return
        self._clear()
        for r in rows:
            self.add(r["created_at"], r["level"], r["category"], r["message"])
