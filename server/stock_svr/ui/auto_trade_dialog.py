"""자동거래 시작 확인창.

현재 모드/주문 게이트/활성 알고리즘/리스크 한도를 요약해 보여주고 시작 여부를 확인한다.
**게이트가 열려 있고 REAL 이면** `START` 를 직접 입력해야 시작 버튼이 활성화된다(오조작 방지).
"""
from __future__ import annotations

import tkinter as tk
from decimal import Decimal, InvalidOperation
from tkinter import ttk

CONFIRM_WORD = "START"


def universe_summary(algo: dict | None) -> str:
    """확인창에 띄울 universe_filter 한 줄 요약."""
    if not algo:
        return "미사용 — 시가총액·주가 제한 없이 모든 종목이 매수 대상입니다"
    from ..algo.params import ParamSet
    from ..algo.universe_filter import SCOPE_COMBINED, UniverseOptions

    opts = UniverseOptions.from_params(
        ParamSet(algo.get("param_defs") or [], algo.get("params") or {}))
    scope = "합산" if opts.rank_scope == SCOPE_COMBINED else "시장별"
    target = "신규 진입만" if not opts.applies_to_avg_down else "신규 진입 + 물타기"
    return (f"사용 — {opts.markets_text} · {scope} 시총 상위 {opts.top_n} · "
            f"최소 주가 {opts.min_price:,}원 ({target}, 매도·손절 제외)")


def collect_start_info(db, account_id: int | None = None) -> dict:
    """확인창에 띄울 정보를 DB 에서 모은다."""
    from ..engine.context import OrderGateState

    settings = db.get_settings()
    gate = OrderGateState.from_settings(settings)
    try:
        algos = db.load_algorithms()
    except Exception:  # noqa: BLE001
        algos = []
    active = [a for a in algos if bool(a.get("is_enabled")) or bool(a.get("is_locked"))]
    entry_like = [a for a in active if a.get("role") in ("entry",)]
    risk = next((a for a in active if a.get("code") == "risk_guard"), None)
    params = (risk or {}).get("params") or {}
    claude = next((a for a in active if a.get("code") == "claude_advisor"), None)
    claude_model = ((claude or {}).get("params") or {}).get("model", "-")
    universe = next((a for a in active if a.get("code") == "universe_filter"), None)
    trend = next((a for a in active if a.get("code") == "claude_trend_scan"), None)
    trend_params = (trend or {}).get("params") or {}
    limit = _limit_preview(db, account_id, params, universe)
    return {
        "trend_on": bool(trend),
        "trend_text": (f"사용 ({trend_params.get('region_scope', '-')}) — "
                       f"조사 시각 {trend_params.get('scan_time', '-')}, 하루 1회"
                       if trend else "미사용 — 산업 트렌드 조사를 하지 않습니다"),
        "claude_on": bool(claude),
        "claude_text": (f"사용 ({claude_model}) — 매수 신호만 검토, 매도·손절은 검토 안 함"
                        if claude else "미사용 — 모든 매수 신호가 그대로 Executor 로 갑니다"),
        "mode": gate.trading_mode.upper(),
        "gate": gate,
        "gate_open": gate.can_send_order,
        "gate_text": gate.describe(),
        "block_reason": gate.block_reason(),
        "active": [(a["code"], a.get("name") or a["code"], a.get("role")) for a in active],
        "has_entry": bool(entry_like),
        "stop_loss_pct": params.get("stop_loss_pct", "-"),
        "max_total_invest": params.get("max_total_invest", "-"),
        "max_invest_per_stock": params.get("max_invest_per_stock", "-"),
        "daily_loss_limit_pct": params.get("daily_loss_limit_pct", "-"),
        "trade_window": f"{params.get('trade_start_time', '-')} ~ {params.get('trade_end_time', '-')}",
        "require_word": gate.can_send_order and gate.trading_mode != "mock",
        "universe_on": bool(universe),
        "universe_text": universe_summary(universe),
        "limit_asset_text": limit.asset_text,
        "limit_per_text": f"{limit.per_limit:,}원 ({limit.per_desc})",
        "limit_warning": limit.warning,
    }


def _limit_preview(db, account_id, risk_params: dict, universe: dict | None):
    """유효 한도(종목당) + 최소 주가 경고. 조회 실패는 '확인 불가'로 처리한다."""
    from ..algo.params import ParamSet
    from ..algo.risk_guard import limit_preview
    from ..algo.universe_filter import UniverseOptions

    asset = None
    if account_id:
        try:
            asset = int((db.latest_balance(account_id) or {}).get("prsm_dpst_aset_amt") or 0)
        except Exception:  # noqa: BLE001
            asset = None
    min_price = 0
    if universe:
        min_price = UniverseOptions.from_params(
            ParamSet(universe.get("param_defs") or [], universe.get("params") or {})).min_price

    def _num(key, default):
        try:
            return Decimal(str(risk_params.get(key, default)))
        except (InvalidOperation, TypeError, ValueError):
            return Decimal(str(default))

    return limit_preview(asset, int(_num("max_total_invest", 0)),
                         _num("max_total_invest_pct", 100),
                         int(_num("max_invest_per_stock", 0)),
                         _num("max_invest_per_stock_pct", 100), min_price)


def _money(value) -> str:
    try:
        return f"{int(value):,}원"
    except (TypeError, ValueError):
        return str(value)


class AutoTradeStartDialog(tk.Toplevel):
    """자동거래 시작 확인 대화상자."""

    def __init__(self, master, info: dict):
        super().__init__(master)
        self.info = info
        self.result = False
        self.title("자동거래 시작 확인")
        self.resizable(False, False)
        self.transient(master)

        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="알고리즘 자동거래를 시작합니다.",
                  font=("맑은 고딕", 12, "bold")).pack(anchor="w")

        # -- 모드 / 게이트 ---------------------------------------------- #
        box = ttk.LabelFrame(frm, text="현재 상태", padding=8)
        box.pack(fill="x", pady=(8, 0))
        self._row(box, 0, "거래 환경", info["mode"],
                  "#c62828" if info["mode"] == "REAL" else "#1565c0")
        if info["gate_open"]:
            self._row(box, 1, "주문 전송", "ON — 신호 발생 시 실제 주문이 전송됩니다", "#c62828")
        else:
            self._row(box, 1, "주문 전송", f"OFF — 관찰모드(신호만 기록) · {info['block_reason']}",
                      "#b26a00")
        self._row(box, 2, "주문 게이트", info["gate_text"], "#555555")
        self._row(box, 3, "Claude 검토", info.get("claude_text", "미사용"),
                  "#1565c0" if info.get("claude_on") else "#777777")
        self._row(box, 4, "종목 유니버스", info.get("universe_text", "미사용"),
                  "#1565c0" if info.get("universe_on") else "#777777")
        self._row(box, 5, "산업 트렌드 스캔", info.get("trend_text", "미사용"),
                  "#1565c0" if info.get("trend_on") else "#777777")

        # -- 알고리즘 ---------------------------------------------------- #
        algo_box = ttk.LabelFrame(frm, text="활성 알고리즘", padding=8)
        algo_box.pack(fill="x", pady=(8, 0))
        if info["active"]:
            for i, (code, name, role) in enumerate(info["active"]):
                role_ko = {"entry": "진입", "risk": "리스크 관리", "filter": "시장 필터"}.get(role, role)
                ttk.Label(algo_box, text=f"· {name}  [{code}] · {role_ko}").grid(
                    row=i, column=0, sticky="w")
        else:
            ttk.Label(algo_box, text="(없음)").grid(row=0, column=0, sticky="w")
        if not info["has_entry"]:
            ttk.Label(algo_box,
                      text="⚠ 진입 알고리즘이 선택되지 않았습니다. 신규 매수 신호가 발생하지 않습니다.\n"
                           "   (알고리즘 탭에서 선택 후 저장하세요.) 그래도 시작할까요?",
                      foreground="#b26a00", justify="left").grid(
                row=len(info["active"]) + 1, column=0, sticky="w", pady=(6, 0))

        # -- 리스크 요약 -------------------------------------------------- #
        risk_box = ttk.LabelFrame(frm, text="리스크 한도 (risk_guard)", padding=8)
        risk_box.pack(fill="x", pady=(8, 0))
        self._row(risk_box, 0, "손절선", f"{info['stop_loss_pct']} %")
        self._row(risk_box, 1, "일 손실 한도", f"{info['daily_loss_limit_pct']} %")
        self._row(risk_box, 2, "총 투입 한도", _money(info["max_total_invest"]))
        self._row(risk_box, 3, "종목당 한도", _money(info["max_invest_per_stock"]))
        self._row(risk_box, 4, "매매 시간", info["trade_window"])
        self._row(risk_box, 5, "총자산(추정예탁)", info.get("limit_asset_text", "확인 불가"))
        self._row(risk_box, 6, "종목당 유효 한도", info.get("limit_per_text", "-"))
        if info.get("limit_warning"):
            ttk.Label(risk_box, text=f"⚠ {info['limit_warning']}", foreground="#c62828",
                      wraplength=520, justify="left").grid(
                row=7, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # -- 확인 입력 ---------------------------------------------------- #
        self.word_var = tk.StringVar()
        if info["require_word"]:
            warn = ttk.LabelFrame(frm, text="실전 주문 전송 확인", padding=8)
            warn.pack(fill="x", pady=(8, 0))
            ttk.Label(warn, text="⚠ 실계좌에 실제 주문이 전송되는 상태입니다.",
                      foreground="#c62828", font=("맑은 고딕", 10, "bold")).pack(anchor="w")
            ttk.Label(warn, text=f"계속하려면 아래에 {CONFIRM_WORD} 을(를) 입력하세요.").pack(anchor="w")
            entry = ttk.Entry(warn, textvariable=self.word_var, width=24)
            entry.pack(anchor="w", pady=4)
            entry.focus_set()
            self.word_var.trace_add("write", lambda *_: self._check())

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(12, 0))
        self.ok_btn = ttk.Button(btns, text="예, 시작합니다", command=self._ok)
        self.ok_btn.pack(side="right")
        ttk.Button(btns, text="아니오", command=self._cancel).pack(side="right", padx=6)
        if info["require_word"]:
            self.ok_btn.configure(state="disabled")

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda e: self._cancel())
        self.grab_set()

    # ------------------------------------------------------------------ #
    def _row(self, parent, row: int, label: str, value: str, color: str = "#000000") -> None:
        ttk.Label(parent, text=f"{label} :").grid(row=row, column=0, sticky="e", padx=(0, 8), pady=1)
        ttk.Label(parent, text=value, foreground=color).grid(row=row, column=1, sticky="w", pady=1)

    def _check(self) -> None:
        ok = self.word_var.get().strip() == CONFIRM_WORD
        self.ok_btn.configure(state="normal" if ok else "disabled")

    def _ok(self) -> None:
        if self.info["require_word"] and self.word_var.get().strip() != CONFIRM_WORD:
            return
        self.result = True
        self.destroy()

    def _cancel(self) -> None:
        self.result = False
        self.destroy()
