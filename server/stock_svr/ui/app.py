"""메인 창 — 상태바 + 4개 탭. UI 스레드는 블로킹하지 않는다(엔진은 별도 스레드)."""
from __future__ import annotations

import datetime as _dt
import logging
import tkinter as tk
from tkinter import messagebox, ttk

from ..config import AppConfig
from ..db import Database
from ..engine.runner import Engine
from .algo_tab import AlgoTab
from .auto_trade_dialog import AutoTradeStartDialog, collect_start_info
from .dashboard_tab import DashboardTab
from .log_tab import LogTab
from .settings_tab import SettingsTab
from .widgets import StatusLight, Tooltip

log = logging.getLogger(__name__)

UI_TICK_MS = 500
DASH_REFRESH_MS = 5000


def smoke_auto_approval(gate_open: bool) -> tuple[bool, str]:
    """스모크(--smoke)에서 자동거래 시작 확인창을 자동 승인해도 되는지 (R-02).

    스모크는 무인 실행이다. 주문 게이트가 열려 있으면 자동 승인이 곧 **실주문**으로
    이어지므로, 그 조합에서는 자동거래를 시작하지 않는다.
    """
    if gate_open:
        return False, ("주문 게이트가 열려 있어 스모크에서는 자동거래를 시작하지 않습니다 "
                       "(무인 실행 중 실주문 방지)")
    return True, ""


class App(tk.Tk):
    def __init__(self, cfg: AppConfig, db: Database, autostart: bool = False,
                 smoke_seconds: float | None = None):
        super().__init__()
        self.cfg = cfg
        self.db = db
        self.engine = Engine(cfg, db)
        self._smoke_seconds = smoke_seconds
        self._closing = False
        self._dash_tick = 0
        self._auto_confirm = False   # 스모크에서만 True (확인창 자동 승인)

        self.title("stock_svr — 키움 자동매매 서버")
        self.geometry("1180x760")
        self.minsize(960, 640)
        try:
            self.option_add("*Font", "맑은고딕 9")
        except tk.TclError:
            pass

        self._build_statusbar()
        self._build_toolbar()
        self._build_tabs()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.after(200, self._first_load)
        self.after(UI_TICK_MS, self._tick)
        if autostart:
            self.after(600, self.start_engine)
        if smoke_seconds:
            # 스모크: 확인창을 자동 승인하고 자동거래 시작 → 중지 토글을 검증한다.
            self._auto_confirm = True
            self.after(int(smoke_seconds * 1000 * 0.35), self._smoke_start_auto)
            self.after(int(smoke_seconds * 1000 * 0.70), self._smoke_stop_auto)
            self.after(int(smoke_seconds * 1000), self._smoke_exit)

    # ================================================================== #
    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self, padding=(10, 6))
        bar.pack(fill="x")
        self.lights: dict[str, StatusLight] = {}
        for key, label in (("db", "DB 접속"), ("kiwoom_rest", "키움 REST"),
                           ("kiwoom_ws", "키움 WS"), ("market", "시장"),
                           ("mode", "모드"), ("order", "주문허용"), ("auto", "자동거래")):
            light = StatusLight(bar, label)
            light.pack(side="left", padx=(0, 16))
            self.lights[key] = light
        self.account_var = tk.StringVar(value="계좌: (미확인)")
        ttk.Label(bar, textvariable=self.account_var).pack(side="right")
        ttk.Separator(self, orient="horizontal").pack(fill="x")

    def _build_toolbar(self) -> None:
        """탭 위 항상 보이는 자동거래 제어 툴바."""
        bar = ttk.Frame(self, padding=(10, 8))
        bar.pack(fill="x")
        self.auto_btn = tk.Button(
            bar, text="▶ 자동거래 시작", command=self.toggle_auto_trading,
            font=("맑은 고딕", 12, "bold"), width=18, height=1,
            bg="#22a36b", fg="white", activebackground="#1b8455", activeforeground="white",
            relief="raised", bd=2, cursor="hand2")
        self.auto_btn.pack(side="left")
        self.auto_btn_tip = Tooltip(self.auto_btn, "알고리즘 평가와 주문을 시작합니다.")

        self.cancel_btn = tk.Button(
            bar, text="긴급 미체결 취소", command=self.cancel_open_orders,
            font=("맑은 고딕", 10), bg="#8d6e63", fg="white",
            disabledforeground="#f5f5f5",
            activebackground="#6d4c41", activeforeground="white", cursor="hand2")
        self.cancel_btn.pack(side="left", padx=(8, 0))
        Tooltip(self.cancel_btn, "미체결 주문을 조회해 전량 취소합니다(주문 게이트·확인 필요).")

        self.auto_state_var = tk.StringVar(value="중지됨")
        self.auto_state_label = tk.Label(bar, textvariable=self.auto_state_var,
                                         font=("맑은 고딕", 11, "bold"), fg="#666666")
        self.auto_state_label.pack(side="left", padx=14)

        self.auto_hint_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.auto_hint_var, foreground="#777777").pack(side="right")
        ttk.Separator(self, orient="horizontal").pack(fill="x")

    def _build_tabs(self) -> None:
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        self.dashboard = DashboardTab(nb, self)
        self.logs = LogTab(nb, self)
        self.algo = AlgoTab(nb, self)
        self.settings = SettingsTab(nb, self)
        nb.add(self.dashboard, text=" 대시보드 ")
        nb.add(self.logs, text=" 주요 기록 ")
        nb.add(self.algo, text=" 알고리즘 ")
        nb.add(self.settings, text=" 설정 ")
        self.notebook = nb

    def _first_load(self) -> None:
        self.settings.reload()
        self.algo.reload()
        self.logs.load_from_db()
        self.dashboard.refresh()
        self._refresh_status()

    # ================================================================== #
    def account_id(self) -> int | None:
        return self.engine.account_id

    def log_event(self, level: str, category: str, message: str) -> None:
        try:
            self.db.log_event(level, category, message)
        except Exception:  # noqa: BLE001
            pass
        self.logs.add(_dt.datetime.now(), level, category, message)

    # ================================================================== #
    def start_engine(self) -> None:
        if self.engine.running:
            return
        self.log_event("INFO", "system", "UI 에서 엔진 시작 요청")
        self.engine.start()
        self._refresh_status()

    def stop_engine(self) -> None:
        if not self.engine.running:
            return
        if self.engine.auto_trading_active:
            self.log_event("WARN", "algo", "엔진(조회·동기화) 정지 요청 → 자동거래도 함께 중지합니다")
        self.log_event("INFO", "system", "UI 에서 엔진(조회·동기화) 정지 요청")
        self.engine.request_stop()
        self._refresh_status()

    # ================================================================== #
    # 자동거래 제어 (알고리즘 평가·주문)
    # ================================================================== #
    def toggle_auto_trading(self) -> None:
        if self.engine.auto_trading_active:
            self._stop_auto_trading()
        else:
            self._start_auto_trading()

    def _start_auto_trading(self) -> None:
        if not self.engine.running:
            messagebox.showwarning(
                "자동거래 시작 불가",
                "엔진(조회·동기화)이 정지 상태입니다.\n설정 탭에서 엔진을 먼저 시작하세요.",
                parent=self)
            return
        try:
            info = collect_start_info(self.db)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("자동거래 시작 불가", f"상태 조회 실패: {exc}", parent=self)
            return

        if self._auto_confirm:
            # R-02: 스모크는 무인 승인이므로 게이트가 열려 있으면 시작 자체를 하지 않는다
            allowed, why = smoke_auto_approval(info["gate_open"])
            if not allowed:
                log.warning("스모크 모드: %s", why)
                self.log_event("WARN", "algo", f"스모크 자동거래 시작 건너뜀 - {why}")
                return
            approved = True
            log.info("스모크 모드: 자동거래 시작 확인창 자동 승인 (게이트 닫힘 확인됨)")
        else:
            dlg = AutoTradeStartDialog(self, info)
            self.wait_window(dlg)
            approved = dlg.result
        if not approved:
            self.log_event("INFO", "algo", "자동거래 시작 취소됨 (UI 확인창)")
            return

        # R-11: 확인창에 보여준 게이트와 지금 게이트를 비교해 더 열렸으면 거부한다
        if self.engine.start_auto_trading(by="UI 버튼", shown_gate=info["gate"]):
            state = "주문 전송 ON" if info["gate_open"] else "관찰(신호만)"
            self.log_event("WARN" if info["gate_open"] else "INFO", "algo",
                           f"자동거래 시작 (UI 버튼, {info['mode']}, {state}, "
                           f"알고리즘 {len(info['active'])}개)")
        elif self.engine.start_refused_reason:
            messagebox.showerror(
                "자동거래 시작 거부",
                f"{self.engine.start_refused_reason}\n\n"
                "설정 탭에서 현재 주문 게이트를 확인한 뒤 다시 시작하세요.", parent=self)
        self._refresh_status()

    def _stop_auto_trading(self, silent: bool = False) -> None:
        open_cnt = self.engine.stop_auto_trading(by="UI 버튼")
        self.log_event("WARN", "algo",
                       f"자동거래 중지 (UI 버튼) - 미체결 주문 {open_cnt}건은 자동 취소되지 않습니다")
        if not silent:
            messagebox.showinfo(
                "자동거래 중지",
                "자동거래를 중지했습니다.\n"
                "· 새 평가 사이클과 신규 주문 전송이 즉시 멈춥니다.\n"
                f"· 미체결 주문 {open_cnt}건은 자동 취소하지 않습니다"
                f"{' (툴바의 긴급 미체결 취소 사용)' if open_cnt else ''}.\n"
                "· 계좌·시세 동기화와 실시간 연결은 계속 동작합니다.",
                parent=self)
        self._refresh_status()

    def cancel_open_orders(self) -> None:
        """긴급 미체결 전량 취소 (주문 게이트 + REAL 확인 필요)."""
        if not self.engine.running or self.engine.executor is None:
            messagebox.showwarning("취소 불가", "엔진(조회·동기화)이 정지 상태입니다.", parent=self)
            return
        gate = self.engine.current_gate()
        if not gate.can_send_order:
            messagebox.showinfo(
                "미체결 취소 불가",
                f"주문 게이트가 닫혀 있어 취소 주문도 전송할 수 없습니다.\n{gate.block_reason()}",
                parent=self)
            return
        if self._auto_confirm:
            log.info("스모크 모드: 긴급 미체결 취소는 건너뜁니다")
            return

        # R-04: ka10075 는 계좌 전체 미체결을 돌려준다 → 대상을 먼저 보여주고 고르게 한다
        try:
            targets = self.engine.list_open_orders()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("미체결 조회 실패", f"{type(exc).__name__}: {exc}", parent=self)
            return
        if not targets:
            messagebox.showinfo("긴급 미체결 취소", "미체결 주문이 없습니다.", parent=self)
            return

        from .cancel_dialog import CancelTargetsDialog
        picker = CancelTargetsDialog(self, targets)
        self.wait_window(picker)
        selected = picker.result
        if not selected:
            self.log_event("INFO", "order", "긴급 미체결 취소 취소됨(대상 미선택)")
            return
        external = [t for t in targets
                    if str(t.get("ord_no")) in set(selected) and not t.get("is_ours")]

        from .settings_tab import ConfirmRealDialog
        if not messagebox.askyesno(
                "긴급 미체결 취소",
                f"선택한 미체결 주문 {len(selected)}건을 취소합니다.\n"
                "⚠ ka10075 는 계좌 전체 미체결을 돌려줍니다 — 이 계좌의 수동(HTS) 주문도 "
                "취소 대상이 될 수 있습니다.\n"
                + (f"⚠ 선택에 외부/수동 주문 {len(external)}건이 포함되어 있습니다.\n"
                   if external else "")
                + "계속할까요?", parent=self):
            return
        dlg = ConfirmRealDialog(self, f"미체결 주문 {len(selected)}건 취소 (kt10003, 계좌 전체 대상)")
        self.wait_window(dlg)
        if not dlg.result:
            self.log_event("WARN", "order", "긴급 미체결 취소 취소됨(확인 실패)")
            return
        result = self.engine.cancel_all_open_orders(by="UI 버튼", only_ord_nos=selected,
                                                    targets=targets)
        messagebox.showinfo(
            "긴급 미체결 취소",
            f"대상 {result['total']}건 / 취소 요청 성공 {result['ok']}건 / "
            f"실패 {result['failed']}건 / 중단 {result.get('skipped', 0)}건"
            + ("\n" + "\n".join(result["errors"][:5]) if result["errors"] else ""),
            parent=self)
        self._refresh_status()

    # -- 스모크 전용 ---------------------------------------------------- #
    def _smoke_start_auto(self) -> None:
        if self._closing:
            return
        log.info("스모크 모드: 자동거래 시작 토글")
        self._start_auto_trading()

    def _smoke_stop_auto(self) -> None:
        if self._closing:
            return
        log.info("스모크 모드: 자동거래 중지 토글")
        self._stop_auto_trading(silent=True)

    # ================================================================== #
    def _tick(self) -> None:
        if self._closing:
            return
        try:
            self.logs.drain_queue()
            self._refresh_status()
            self._dash_tick += UI_TICK_MS
            if self._dash_tick >= DASH_REFRESH_MS:
                self._dash_tick = 0
                self.dashboard.refresh()
        except Exception:  # noqa: BLE001 - UI 루프를 죽이지 않는다
            log.debug("UI tick 오류", exc_info=True)
        finally:
            self.after(UI_TICK_MS, self._tick)

    def _refresh_status(self) -> None:
        s = self.engine.status.snapshot()
        for key in ("db", "kiwoom_rest", "kiwoom_ws", "market"):
            st, msg = s[key]
            label = {"db": "DB 접속", "kiwoom_rest": "키움 REST",
                     "kiwoom_ws": "키움 WS", "market": "시장"}[key]
            text = {"db": "DB", "kiwoom_rest": "REST", "kiwoom_ws": "WS", "market": "시장"}[key]
            show = {"ok": "정상", "warn": "주의", "error": "오류", "unknown": "미확인"}.get(st, st)
            if key == "market":
                show = msg or show
            self.lights[key].label_text = label
            self.lights[key].update_state(st, f"{text}: {show}", msg)

        mode = s["mode"]
        self.lights["mode"].label_text = "모드"
        self.lights["mode"].update_state("warn" if mode == "REAL" else "ok",
                                         f"모드: {mode}", s["gate_text"])
        allowed = s["order_allowed"]
        self.lights["order"].label_text = "주문허용"
        if allowed and mode == "REAL":
            self.lights["order"].update_state("alert", "주문: 실전 ON ⚠", s["gate_text"])
        elif allowed:
            self.lights["order"].update_state("ok", "주문: ON", s["gate_text"])
        else:
            self.lights["order"].update_state("unknown", "주문: OFF(관찰)", s["gate_text"])

        # -- 자동거래 상태(상태바 아이콘 + 툴바) -------------------------- #
        auto_on = s["auto_trading"]
        auto_st, auto_msg = s["auto_status"]
        self.lights["auto"].label_text = "자동거래"
        self.lights["auto"].update_state(
            "alert" if (auto_on and allowed and mode == "REAL") else auto_st,
            f"자동거래: {'실행중' if auto_on else '중지됨'}", auto_msg)
        self._refresh_auto_toolbar(auto_on, allowed, mode, s["running"], auto_msg)

        self.account_var.set(
            f"계좌: {s['account_no_masked']}   사이클 {s['cycles']}  신호 {s['signals']}  "
            f"전송 {s['orders_sent']}   최근 {s['last_cycle_at'] or '-'}")
        self.settings.refresh_engine_state(s["running"], s["gate_text"])

    def _refresh_auto_toolbar(self, auto_on: bool, order_allowed: bool, mode: str,
                              engine_running: bool, auto_msg: str) -> None:
        if auto_on:
            self.auto_btn.configure(text="■ 자동거래 중지", bg="#d64545",
                                    activebackground="#b23636", state="normal")
            self.auto_btn_tip.set("알고리즘 평가와 신규 주문 전송을 즉시 중지합니다.")
            if order_allowed:
                self.auto_state_var.set(
                    f"실행중 · 주문 전송 ON{' ⚠ 실계좌' if mode == 'REAL' else ''}")
                self.auto_state_label.configure(fg="#c62828")
            else:
                self.auto_state_var.set("실행중 · 관찰(신호만, 주문 미전송)")
                self.auto_state_label.configure(fg="#b26a00")
        else:
            self.auto_btn.configure(text="▶ 자동거래 시작", bg="#22a36b",
                                    activebackground="#1b8455",
                                    state="normal" if engine_running else "disabled")
            self.auto_state_var.set("중지됨")
            self.auto_state_label.configure(fg="#666666")
            self.auto_btn_tip.set(
                "알고리즘 평가와 주문을 시작합니다."
                if engine_running else
                "엔진(조회·동기화)이 정지 상태입니다. 설정 탭에서 엔진을 먼저 시작하세요.")
        cancel_ok = engine_running and order_allowed
        self.cancel_btn.configure(state="normal" if cancel_ok else "disabled",
                                  bg="#8d6e63" if cancel_ok else "#9e9e9e")
        self.auto_hint_var.set(
            "엔진(조회·동기화)은 자동거래와 무관하게 계속 동작합니다" if engine_running
            else "엔진(조회·동기화) 정지 상태")

    # ================================================================== #
    def _smoke_exit(self) -> None:
        log.info("스모크 모드: 자동 종료")
        self._shutdown(confirm=False)

    def on_close(self) -> None:
        if self.engine.running:
            extra = ""
            if self.engine.auto_trading_active:
                open_cnt = self.engine.count_open_orders()
                extra = ("\n\n⚠ 자동거래가 실행 중입니다. 종료하면 자동거래도 함께 중지됩니다."
                         f"\n   미체결 주문 {open_cnt}건은 자동 취소되지 않습니다.")
            if not messagebox.askokcancel(
                    "종료 확인",
                    "엔진(조회·동기화)이 실행 중입니다.\n"
                    "정상 종료(실시간 해제·토큰 폐기)를 진행할까요?" + extra,
                    parent=self):
                return
        elif not messagebox.askokcancel("종료 확인", "stock_svr 를 종료할까요?", parent=self):
            return
        self._shutdown(confirm=False)

    def _shutdown(self, confirm: bool = True) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            if self.engine.running:
                self.engine.stop(timeout=20)
        except Exception:  # noqa: BLE001
            log.exception("엔진 종료 실패")
        try:
            self.db.set_status("server", "unknown", "UI 종료")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.destroy()
        except Exception:  # noqa: BLE001
            pass


def run_ui(cfg: AppConfig, db: Database, autostart: bool = False,
           smoke_seconds: float | None = None) -> int:
    app = App(cfg, db, autostart=autostart, smoke_seconds=smoke_seconds)
    app.mainloop()
    return 0
