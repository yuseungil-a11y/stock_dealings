"""④ 설정 — 거래환경/주문허용/실전 이중확인/평가주기 + 엔진 시작·정지.

실전(trading_mode=real)에서 주문을 켜려면 확인창에 `REAL` 을 직접 입력해야 한다.
"""
from __future__ import annotations

import logging
import threading
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

        fin_box = ttk.LabelFrame(
            self, text="기업 재무분석 대상 (DART, 참고용 — 매매와 무관, 자세한 내용은 리서치 화면 참조)",
            padding=10)
        fin_box.pack(fill="x", pady=(12, 0))

        ttk.Label(fin_box, text="시장별 대상 상위 N:").grid(row=0, column=0, sticky="e", padx=(0, 6), pady=4)
        self.fin_top_n_var = tk.StringVar(value="100")
        ttk.Spinbox(fin_box, from_=1, to=500, textvariable=self.fin_top_n_var, width=10).grid(
            row=0, column=1, sticky="w", pady=4)
        ttk.Label(fin_box, text="코스피·코스닥 각각 시가총액 순위 상위 N개 기업을 재무분석 대상으로 삼는다"
                                "(universe_filter 와 동일한 순위 로직 재사용). 기본 100",
                  foreground="#777777").grid(row=0, column=2, sticky="w", padx=(10, 0))

        ttk.Label(fin_box, text="수집 연수:").grid(row=1, column=0, sticky="e", padx=(0, 6), pady=4)
        self.fin_years_var = tk.StringVar(value="5")
        ttk.Spinbox(fin_box, from_=1, to=10, textvariable=self.fin_years_var, width=10).grid(
            row=1, column=1, sticky="w", pady=4)
        ttk.Label(fin_box, text="몇 년치 분기·반기·사업보고서를 받을지", foreground="#777777").grid(
            row=1, column=2, sticky="w", padx=(10, 0))

        ttk.Label(fin_box, text="하루 Claude 리포트 상한:").grid(row=2, column=0, sticky="e", padx=(0, 6), pady=4)
        self.fin_report_limit_var = tk.StringVar(value="3")
        ttk.Spinbox(fin_box, from_=0, to=50, textvariable=self.fin_report_limit_var, width=10).grid(
            row=2, column=1, sticky="w", pady=4)
        ttk.Label(fin_box, text="하루에 새로 생성할 Claude 해설 리포트 건수 상한(0이면 리포트 생성 끔 — "
                                "PER/PBR/ROE 수집·계산은 계속됨, Claude 비용과 직결)",
                  foreground="#777777").grid(row=2, column=2, sticky="w", padx=(10, 0))

        ttk.Label(fin_box, text="하루 DART 호출 상한:").grid(row=3, column=0, sticky="e", padx=(0, 6), pady=4)
        self.fin_max_fetch_var = tk.StringVar(value="300")
        ttk.Spinbox(fin_box, from_=0, to=5000, textvariable=self.fin_max_fetch_var, width=10).grid(
            row=3, column=1, sticky="w", pady=4)
        ttk.Label(fin_box, text="DART 는 무료이나 전체 대상을 한 번에 못 받을 수 있어 며칠에 걸쳐 백필된다",
                  foreground="#777777").grid(row=3, column=2, sticky="w", padx=(10, 0))

        ttk.Label(fin_box, text="실행 시각(시):").grid(row=4, column=0, sticky="e", padx=(0, 6), pady=4)
        self.fin_run_hour_var = tk.StringVar(value="16")
        ttk.Spinbox(fin_box, from_=0, to=23, textvariable=self.fin_run_hour_var, width=10).grid(
            row=4, column=1, sticky="w", pady=4)
        ttk.Label(fin_box, text="장마감 후 하루 1회 자동 실행하는 시각", foreground="#777777").grid(
            row=4, column=2, sticky="w", padx=(10, 0))

        btns2 = ttk.Frame(fin_box)
        btns2.grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Button(btns2, text="재무분석 설정 저장", command=self.save_fundamentals).pack(side="left")
        self.fin_run_btn = ttk.Button(btns2, text="지금 수집 실행 (최대 3개)",
                                      command=self.run_fundamentals_now)
        self.fin_run_btn.pack(side="left", padx=(6, 0))
        ttk.Label(fin_box,
                  text="하루 자동 실행(장마감 후 1회)과 별개로, 원할 때 직접 최대 3개 종목을 즉시 수집·"
                       "리포트 생성한다(DART+Claude 호출, 수 분 걸릴 수 있음). 오늘 이미 리포트가 있는 "
                       "종목은 건너뛰고 다음 종목으로 넘어간다 — 여러 번 누르면 대상 종목을 순서대로 채워나간다.",
                  foreground="#777777", wraplength=640, justify="left").grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.fin_run_status_var = tk.StringVar(value="")
        ttk.Label(fin_box, textvariable=self.fin_run_status_var, foreground="#1565c0").grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(4, 0))

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
            self.fin_top_n_var.set(s.get("fundamentals_top_n", "100"))
            self.fin_years_var.set(s.get("fundamentals_years", "5"))
            self.fin_report_limit_var.set(s.get("fundamentals_report_limit", "3"))
            self.fin_max_fetch_var.set(s.get("fundamentals_max_fetch", "300"))
            self.fin_run_hour_var.set(s.get("fundamentals_run_hour", "16"))
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

    def save_fundamentals(self) -> None:
        """기업 재무분석(DART, 참고용) 대상·주기 설정 저장.

        게이트 3키가 아니므로 REAL 확인 없이 일반 `set_setting` 으로 저장한다.
        매매 파이프라인은 이 값을 전혀 읽지 않는다(재무분석은 매매와 완전 분리).
        """
        db = self.app.db
        if not db:
            return
        fields = (
            ("fundamentals_top_n", self.fin_top_n_var, 1, 500),
            ("fundamentals_years", self.fin_years_var, 1, 10),
            ("fundamentals_report_limit", self.fin_report_limit_var, 0, 50),
            ("fundamentals_max_fetch", self.fin_max_fetch_var, 0, 5000),
            ("fundamentals_run_hour", self.fin_run_hour_var, 0, 23),
        )
        current = db.get_settings()
        changed = []
        for key, var, lo, hi in fields:
            try:
                val = max(lo, min(hi, int(str(var.get()).strip())))
            except (TypeError, ValueError):
                messagebox.showerror("설정 저장 실패", f"{key}: 숫자를 입력하세요.", parent=self)
                return
            var.set(str(val))
            if current.get(key) == str(val):
                continue
            try:
                db.set_setting(key, str(val), updated_by="ui")
            except Exception as exc:  # noqa: BLE001 - 한 항목 실패가 나머지를 막지 않게
                log.exception("재무분석 설정 저장 실패: %s", key)
                messagebox.showerror("설정 저장 실패", f"{key}: {exc}", parent=self)
                continue
            changed.append(f"{key}={val}")
        if changed:
            self.app.log_event("INFO", "system", "재무분석 설정 변경: " + ", ".join(changed))
            messagebox.showinfo("설정 저장", "저장되었습니다.\n" + "\n".join(changed), parent=self)
        self.reload()

    def run_fundamentals_now(self) -> None:
        """"지금 수집 실행" 버튼 — 하루 자동 실행(16시 1회)과 별개로 수동 즉시 실행.

        네트워크(DART+Claude) 호출이 수 분 걸릴 수 있어 별도 스레드에서 돌리고,
        UI 갱신은 `self.after(0, ...)` 로 메인 스레드에 넘긴다(universe_preview.py 와 동일 패턴).
        매매 파이프라인(게이트·algorithm_selection·signal_log·orders·Executor·risk_guard)은
        전혀 건드리지 않는다 — `FundamentalsService.run_once()` 는 참고용 재무 리포트만 만든다.
        """
        db = self.app.db
        cfg = self.app.cfg
        if not db:
            return
        if not cfg.dart.configured:
            messagebox.showerror("실행 불가", "[dart] apikey_file 이 설정되지 않았습니다.", parent=self)
            return
        if not cfg.anthropic.configured:
            messagebox.showerror("실행 불가", "[anthropic] apikey_file 이 설정되지 않았습니다.", parent=self)
            return

        self.fin_run_btn.configure(state="disabled")
        self.fin_run_status_var.set("실행 중... (DART+Claude 호출, 수 분 걸릴 수 있습니다)")

        def work() -> None:
            from ..services.fundamentals import FundamentalsService

            svc = FundamentalsService(db, dart_cfg=cfg.dart, anthropic_cfg=cfg.anthropic)
            try:
                result = svc.run_once(report_limit=3)
                msg = self._format_result(result)
                err: Exception | None = None
            except Exception as exc:  # noqa: BLE001 - 실행 실패를 UI 로 보고하고 앱은 죽지 않게
                log.exception("재무분석 수동 실행 실패")
                msg = ""
                err = exc
            finally:
                svc.close()
            try:
                self.after(0, lambda: self._on_fundamentals_done(msg, err))
            except tk.TclError:
                pass        # 창이 이미 닫힘

        threading.Thread(target=work, name="fundamentals-run-now", daemon=True).start()

    @staticmethod
    def _format_result(result) -> str:
        f = result.fetch
        reports = result.reports or []
        ok = sum(1 for r in reports if r.get("status") == "ok")
        err = sum(1 for r in reports if r.get("status") != "ok")
        names = ", ".join(f"{r.get('stk_nm') or r.get('stk_cd')}" for r in reports) or "(없음 - 오늘 대상 종목 소진 또는 전부 이미 완료)"
        return (f"대상 {len(result.targets)}종목 중 리포트 {len(reports)}건 생성"
                f"(정상 {ok} / 오류 {err})  -  {names}  |  DART 호출 {f.get('calls', 0)}회")

    def _on_fundamentals_done(self, msg: str, err: Exception | None) -> None:
        if self.winfo_exists():
            self.fin_run_btn.configure(state="normal")
            self.fin_run_status_var.set(
                f"실패: {type(err).__name__}: {err}" if err else msg)
        if err is None:
            self.app.log_event("INFO", "system", f"재무분석 수동 실행: {msg}")
        else:
            self.app.log_event("ERROR", "system", f"재무분석 수동 실행 실패: {err}")

    def refresh_engine_state(self, running: bool, gate_text: str = "") -> None:
        self.engine_var.set(("실행 중" if running else "정지됨") + (f"   ({gate_text})" if gate_text else ""))
        self.start_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
