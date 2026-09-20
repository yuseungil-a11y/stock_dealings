"""④ 설정 — 거래환경/주문허용/실전 이중확인/평가주기 + 엔진 시작·정지.

실전(trading_mode=real)에서 주문을 켜려면 확인창에 `REAL` 을 직접 입력해야 한다.
"""
from __future__ import annotations

import logging
import tkinter as tk
from tkinter import messagebox, ttk

log = logging.getLogger(__name__)

CONFIRM_WORD = "REAL"


def gate_turns_on(current: dict, proposed: dict) -> tuple[bool, str]:
    """게이트 결과가 OFF→ON 으로 바뀌고 그 결과가 실계좌인지 판정한다 (S-03).

    반환: (확인창 필요 여부, 변경 요약). 순수 함수이므로 단위테스트로 검증한다.
    """
    from ..engine.context import OrderGateState

    before = OrderGateState.from_settings(current)
    after = OrderGateState.from_settings(proposed)
    if not after.can_send_order:
        return False, ""
    if before.can_send_order and before.trading_mode == after.trading_mode:
        return False, ""          # 이미 켜져 있고 환경도 그대로면 재확인 불필요
    if after.trading_mode != "real":
        return False, ""          # 모의투자는 확인창 없이 허용
    diffs = [f"{k}: {current.get(k, '-')} → {v}"
             for k, v in proposed.items() if current.get(k) != v]
    return True, "실계좌 주문 전송 ON  (" + ", ".join(diffs or ["게이트 ON"]) + ")"


class ConfirmRealDialog(tk.Toplevel):
    """실전 주문 활성화 확인창 — 'REAL' 을 입력해야 확인 버튼이 활성화된다."""

    def __init__(self, master, what: str):
        super().__init__(master)
        self.title("실전 주문 활성화 확인")
        self.resizable(False, False)
        self.transient(master)
        self.result = False
        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="⚠ 실계좌 주문이 실제로 전송됩니다.",
                  font=("맑은 고딕", 11, "bold"), foreground="#c62828").pack(anchor="w")
        ttk.Label(frm, text=f"변경 항목: {what}", padding=(0, 6)).pack(anchor="w")
        ttk.Label(frm, text=f"계속하려면 아래에 {CONFIRM_WORD} 을(를) 입력하세요.").pack(anchor="w")
        self.var = tk.StringVar()
        entry = ttk.Entry(frm, textvariable=self.var, width=24)
        entry.pack(anchor="w", pady=6)
        entry.focus_set()
        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(8, 0))
        self.ok_btn = ttk.Button(btns, text="활성화", command=self._ok, state="disabled")
        self.ok_btn.pack(side="right")
        ttk.Button(btns, text="취소", command=self._cancel).pack(side="right", padx=6)
        self.var.trace_add("write", lambda *_: self._check())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda e: self._cancel())
        self.grab_set()

    def _check(self) -> None:
        self.ok_btn.configure(state="normal" if self.var.get().strip() == CONFIRM_WORD else "disabled")

    def _ok(self) -> None:
        if self.var.get().strip() == CONFIRM_WORD:
            self.result = True
            self.destroy()

    def _cancel(self) -> None:
        self.result = False
        self.destroy()


class SettingsTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=10)
        self.app = app
        self._loading = False

        box = ttk.LabelFrame(self, text="거래 환경 (DB system_setting)", padding=10)
        box.pack(fill="x")

        ttk.Label(box, text="거래 환경:").grid(row=0, column=0, sticky="e", padx=(0, 6), pady=4)
        self.mode_var = tk.StringVar(value="real")
        self.mode_combo = ttk.Combobox(box, textvariable=self.mode_var, state="readonly", width=14,
                                       values=["real - 실계좌", "mock - 모의투자"])
        self.mode_combo.grid(row=0, column=1, sticky="w", pady=4)

        self.order_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="주문 전송 허용 (order_enabled)",
                        variable=self.order_var).grid(row=1, column=0, columnspan=2, sticky="w", pady=4)
        self.confirm_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="실계좌 주문 이중확인 (real_trading_confirm)",
                        variable=self.confirm_var).grid(row=2, column=0, columnspan=2, sticky="w", pady=4)

        ttk.Label(box, text="평가 주기(초):").grid(row=3, column=0, sticky="e", padx=(0, 6), pady=4)
        self.poll_var = tk.StringVar(value="30")
        ttk.Spinbox(box, from_=5, to=3600, textvariable=self.poll_var, width=10).grid(
            row=3, column=1, sticky="w", pady=4)

        ttk.Label(box, text="로그 보관(일):").grid(row=4, column=0, sticky="e", padx=(0, 6), pady=4)
        self.retention_var = tk.StringVar(value="7")
        ttk.Spinbox(box, from_=1, to=90, textvariable=self.retention_var, width=10).grid(
            row=4, column=1, sticky="w", pady=4)

        self.gate_var = tk.StringVar(value="")
        ttk.Label(box, textvariable=self.gate_var, foreground="#1565c0").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))

        btns = ttk.Frame(box)
        btns.grid(row=6, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Button(btns, text="설정 저장", command=self.save).pack(side="left")
        ttk.Button(btns, text="다시 읽기", command=self.reload).pack(side="left", padx=6)

        engine_box = ttk.LabelFrame(self, text="엔진(조회·동기화)", padding=10)
        engine_box.pack(fill="x", pady=(12, 0))
        self.start_btn = ttk.Button(engine_box, text="엔진(조회·동기화) 시작",
                                    command=self.app.start_engine)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(engine_box, text="엔진(조회·동기화) 정지",
                                   command=self.app.stop_engine)
        self.stop_btn.pack(side="left", padx=6)
        self.engine_var = tk.StringVar(value="정지됨")
        ttk.Label(engine_box, textvariable=self.engine_var).pack(side="left", padx=12)
        ttk.Label(engine_box,
                  text="※ 알고리즘 자동거래 시작/중지는 상단 툴바의 버튼으로 제어합니다.\n"
                       "   엔진을 정지하면 자동거래도 함께 중지됩니다.",
                  foreground="#777777", justify="left").pack(side="left", padx=12)

        info = ttk.LabelFrame(self, text="설정 파일 (읽기 전용)", padding=10)
        info.pack(fill="both", expand=True, pady=(12, 0))
        self.info_var = tk.StringVar(value="")
        ttk.Label(info, textvariable=self.info_var, justify="left").pack(anchor="w")

    # ================================================================== #
    def reload(self) -> None:
        db = self.app.db
        if not db:
            return
        self._loading = True
        try:
            s = db.get_settings()
            mode = (s.get("trading_mode") or "real").lower()
            self.mode_var.set("mock - 모의투자" if mode == "mock" else "real - 실계좌")
            self.order_var.set(s.get("order_enabled") == "1")
            self.confirm_var.set(s.get("real_trading_confirm") == "1")
            self.poll_var.set(s.get("poll_interval_sec", "30"))
            self.retention_var.set(s.get("log_retention_days", "7"))
            self._update_gate_text(s)
        except Exception:  # noqa: BLE001
            log.exception("설정 로드 실패")
        finally:
            self._loading = False
        cfg = self.app.cfg
        self.info_var.set(
            f"설정 파일 : {cfg.source_path}\n"
            f"DB        : {cfg.db.user}@{cfg.db.host}:{cfg.db.port}/{cfg.db.name}\n"
            f"로그 경로 : {cfg.logging.path}  (레벨 {cfg.logging.level}, 보관 {cfg.logging.retention_days}일)\n"
            f"키 파일   : 설정파일의 경로에서 읽으며 값은 화면·로그에 표시하지 않습니다.")

    def _update_gate_text(self, s: dict) -> None:
        from ..engine.context import OrderGateState
        gate = OrderGateState.from_settings(s)
        self.gate_var.set(
            f"현재 주문 게이트: {gate.describe()}"
            + ("" if gate.can_send_order else f"  → 신호만 기록 ({gate.block_reason()})"))

    def _mode_value(self) -> str:
        return "mock" if self.mode_var.get().startswith("mock") else "real"

    # ================================================================== #
    def save(self) -> None:
        db = self.app.db
        if not db:
            return
        current = db.get_settings()
        mode = self._mode_value()
        order_enabled = bool(self.order_var.get())
        confirm = bool(self.confirm_var.get())

        # 실전에서 주문을 켜는 방향의 변경은 REAL 입력 확인 필요
        # S-03: '게이트 결과'가 OFF→ON 이 되는 **모든** 저장에 REAL 확인을 요구한다.
        # (mock 에서 두 스위치를 켜 둔 뒤 real 로 전환하는 우회도 여기서 걸린다.)
        proposed = {
            "trading_mode": mode,
            "order_enabled": "1" if order_enabled else "0",
            "real_trading_confirm": "1" if confirm else "0",
        }
        need_confirm, what = gate_turns_on(current, proposed)
        if need_confirm:
            dlg = ConfirmRealDialog(self.winfo_toplevel(), what)
            self.wait_window(dlg)
            if not dlg.result:
                self.app.log_event("WARN", "system", "실전 주문 활성화 취소됨(확인 실패)")
                self.reload()
                return

        try:
            poll = max(5, int(self.poll_var.get()))
        except (TypeError, ValueError):
            poll = 30
        try:
            retention = max(1, int(self.retention_var.get()))
        except (TypeError, ValueError):
            retention = 7

        # R-10: 게이트 3키는 전용 set_gate() 로만 저장한다(일반 set_setting 은 예외를 던진다).
        #       여기까지 온 경로는 gate_turns_on() 검사 + (필요 시) REAL 확인을 통과한 경로다.
        from ..db import GATE_CONFIRM_TOKEN, GATE_KEYS

        changes = {
            "trading_mode": mode,
            "order_enabled": "1" if order_enabled else "0",
            "real_trading_confirm": "1" if confirm else "0",
            "poll_interval_sec": str(poll),
            "log_retention_days": str(retention),
        }
        changed = []
        for key, val in changes.items():
            if current.get(key) == val:
                continue
            try:
                if key in GATE_KEYS:
                    db.set_gate(key, val, confirm_token=GATE_CONFIRM_TOKEN, updated_by="ui")
                else:
                    db.set_setting(key, val, updated_by="ui")
            except Exception as exc:  # noqa: BLE001 - 한 항목 실패가 나머지를 막지 않게
                log.exception("설정 저장 실패: %s", key)
                messagebox.showerror("설정 저장 실패", f"{key}: {exc}", parent=self)
                continue
            changed.append(f"{key}={val}")
        if changed:
            self.app.log_event("WARN" if "order_enabled=1" in changed else "INFO",
                               "system", "설정 변경: " + ", ".join(changed))
            messagebox.showinfo("설정 저장", "저장되었습니다.\n" + "\n".join(changed), parent=self)
        self.reload()

    def refresh_engine_state(self, running: bool, gate_text: str = "") -> None:
        self.engine_var.set(("실행 중" if running else "정지됨") + (f"   ({gate_text})" if gate_text else ""))
        self.start_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
