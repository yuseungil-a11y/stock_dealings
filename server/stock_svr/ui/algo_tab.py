"""③ 알고리즘 — 선택/우선순위 + `algorithm_param_def` 기반 동적 파라미터 편집 폼."""
from __future__ import annotations

import logging
import tkinter as tk
from tkinter import messagebox, ttk

from ..algo import registry
from ..algo.params import ParamError, enum_choices, validate

log = logging.getLogger(__name__)


class AlgoTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=6)
        self.app = app
        self.algos: list[dict] = []
        self.selected_id: int | None = None
        self._enabled_vars: dict[int, tk.BooleanVar] = {}
        self._priority_vars: dict[int, tk.StringVar] = {}
        self._param_widgets: dict[str, tuple[dict, tk.Variable]] = {}

        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True)

        left = ttk.LabelFrame(paned, text="알고리즘 선택 / 우선순위", padding=6)
        paned.add(left, weight=1)
        self.list_frame = ttk.Frame(left)
        self.list_frame.pack(fill="both", expand=True)
        ttk.Button(left, text="선택/우선순위 저장", command=self.save_selection).pack(fill="x", pady=(6, 0))
        ttk.Button(left, text="새로고침", command=self.reload).pack(fill="x", pady=(4, 0))

        right = ttk.LabelFrame(paned, text="파라미터", padding=6)
        paned.add(right, weight=2)
        self.title_var = tk.StringVar(value="알고리즘을 선택하세요")
        ttk.Label(right, textvariable=self.title_var, font=("맑은 고딕", 11, "bold")).pack(anchor="w")
        self.desc_var = tk.StringVar(value="")
        self.desc_label = ttk.Label(right, textvariable=self.desc_var, wraplength=520,
                                    foreground="#555555", justify="left")
        self.desc_label.pack(anchor="w", fill="x", pady=(2, 6))
        # 창 폭이 바뀌면 알고리즘 설명도 그 폭에 맞춰 줄바꿈
        right.bind("<Configure>",
                   lambda e: self.desc_label.configure(wraplength=max(200, e.width - 30)))

        canvas_wrap = ttk.Frame(right)
        canvas_wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(canvas_wrap, highlightthickness=0)
        ybar = ttk.Scrollbar(canvas_wrap, orient="vertical", command=self.canvas.yview)
        self.form = ttk.Frame(self.canvas)
        self.form.bind("<Configure>",
                       lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._form_win = self.canvas.create_window((0, 0), window=self.form, anchor="nw")
        # 폼 폭을 캔버스 폭에 고정(가로로 잘리지 않게)하고, 설명 열이 남는 폭을 쓰도록 한다
        self.form.columnconfigure(3, weight=1)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.configure(yscrollcommand=ybar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        ybar.pack(side="right", fill="y")
        self._desc_labels: list[ttk.Label] = []

        btns = ttk.Frame(right)
        btns.pack(fill="x", pady=(6, 0))
        ttk.Button(btns, text="파라미터 저장", command=self.save_params).pack(side="left")
        ttk.Button(btns, text="기본값 복원", command=self.restore_defaults).pack(side="left", padx=6)
        self.status_var = tk.StringVar(value="")
        ttk.Label(btns, textvariable=self.status_var, foreground="#1565c0").pack(side="left", padx=10)

    # ================================================================== #
    def reload(self) -> None:
        db = self.app.db
        if not db:
            return
        try:
            self.algos = db.load_algorithms()
        except Exception:  # noqa: BLE001
            log.exception("알고리즘 목록 로드 실패")
            return
        known = set(registry.known_codes())
        for w in self.list_frame.winfo_children():
            w.destroy()
        self._enabled_vars.clear()
        self._priority_vars.clear()

        hdr = ttk.Frame(self.list_frame)
        hdr.pack(fill="x")
        ttk.Label(hdr, text="사용", width=5).pack(side="left")
        ttk.Label(hdr, text="우선", width=6).pack(side="left")
        ttk.Label(hdr, text="알고리즘").pack(side="left")

        for a in self.algos:
            row = ttk.Frame(self.list_frame)
            row.pack(fill="x", pady=1)
            enabled = tk.BooleanVar(value=bool(a["is_enabled"]) or bool(a["is_locked"]))
            self._enabled_vars[a["id"]] = enabled
            chk = ttk.Checkbutton(row, variable=enabled, width=3)
            chk.pack(side="left")
            if a["is_locked"]:
                chk.state(["disabled"])
            pv = tk.StringVar(value=str(a["priority"]))
            self._priority_vars[a["id"]] = pv
            ttk.Spinbox(row, from_=1, to=999, textvariable=pv, width=5).pack(side="left", padx=(0, 6))
            missing = "" if a["code"] in known else "  (구현 없음)"
            lock = " 🔒" if a["is_locked"] else ""
            btn = ttk.Button(row, text=f"{a['name']} [{a['code']}]{lock}{missing}",
                             command=lambda aid=a["id"]: self.select(aid), width=34)
            btn.pack(side="left", fill="x", expand=True)

        if self.algos and self.selected_id is None:
            self.select(self.algos[0]["id"])
        elif self.selected_id is not None:
            self.select(self.selected_id)

    # ------------------------------------------------------------------ #
    def _algo(self, algo_id: int) -> dict | None:
        return next((a for a in self.algos if a["id"] == algo_id), None)

    def select(self, algo_id: int) -> None:
        self.selected_id = algo_id
        a = self._algo(algo_id)
        if not a:
            return
        role_ko = {"entry": "진입", "risk": "리스크 관리", "filter": "시장 필터"}.get(a["role"], a["role"])
        self.title_var.set(f"{a['name']}  [{a['code']}] · {role_ko}")
        self.desc_var.set(a.get("description") or "")
        for w in self.form.winfo_children():
            w.destroy()
        self._param_widgets.clear()
        self._desc_labels.clear()

        defs = a.get("param_defs") or []
        if not defs:
            ttk.Label(self.form, text="(편집할 파라미터가 없습니다)").grid(row=0, column=0, sticky="w")
            return
        # 파라미터 1개 = 2줄: [라벨 | 입력칸] 아래에 [단위·범위·기본값 — 설명] 을 폭에 맞춰 줄바꿈
        # (창을 좁혀도 오른쪽이 잘리지 않도록 한 줄에 여러 열을 두지 않는다)
        for i, d in enumerate(defs):
            r = i * 2
            ttk.Label(self.form, text=f"{d['label']}").grid(
                row=r, column=0, sticky="e", padx=(0, 6), pady=(6, 0))
            var, widget = self._make_widget(d, a["params"].get(d["param_key"], d["default_value"]))
            widget.grid(row=r, column=1, sticky="w", pady=(6, 0))
            self._param_widgets[d["param_key"]] = (d, var)
            hint = []
            if d.get("unit"):
                hint.append(d["unit"])
            if d.get("min_value") not in (None, "") or d.get("max_value") not in (None, ""):
                hint.append(f"{d.get('min_value', '')}~{d.get('max_value', '')}")
            hint.append(f"기본 {d['default_value']}")
            text = " / ".join(hint)
            if d.get("description"):
                text += f"  —  {d['description']}"
            lbl = ttk.Label(self.form, text=text, foreground="#777777", wraplength=360, justify="left")
            lbl.grid(row=r + 1, column=1, columnspan=2, sticky="w", padx=(0, 6), pady=(0, 2))
            self._desc_labels.append(lbl)
        self.after_idle(self._fit_desc_width)

    # ------------------------------------------------------------------ #
    def _on_canvas_configure(self, event) -> None:
        """캔버스 폭 변경 시 폼 폭을 맞추고 설명 열의 줄바꿈 폭을 다시 계산한다."""
        self.canvas.itemconfigure(self._form_win, width=event.width)
        self._fit_desc_width()

    def _fit_desc_width(self) -> None:
        if not self._desc_labels:
            return
        try:
            self.form.update_idletasks()
            fixed = self.form.grid_bbox(0, 0)[2]  # 라벨 열 폭 (설명은 그 오른쪽 전체를 사용)
            wrap = max(160, self.canvas.winfo_width() - fixed - 30)
            for lbl in self._desc_labels:
                if lbl.winfo_exists():
                    lbl.configure(wraplength=wrap)
        except tk.TclError:
            pass

    def _make_widget(self, d: dict, value) -> tuple[tk.Variable, tk.Widget]:
        vtype = (d.get("value_type") or "string").lower()
        if vtype == "bool":
            var = tk.BooleanVar(value=str(value).strip() in ("1", "true", "True", "Y", "y"))
            return var, ttk.Checkbutton(self.form, variable=var)
        if vtype == "enum":
            choices = enum_choices(d)
            labels = [f"{v} - {lab}" if lab != v else v for v, lab in choices]
            var = tk.StringVar(value=str(value))
            combo = ttk.Combobox(self.form, values=labels, state="readonly", width=26)
            cur = str(value)
            idx = next((i for i, (v, _) in enumerate(choices) if v == cur), 0)
            if labels:
                combo.current(idx)
            combo._choices = choices  # type: ignore[attr-defined]
            combo.bind("<<ComboboxSelected>>",
                       lambda e, c=combo, v=var: v.set(c._choices[c.current()][0]))
            return var, combo
        var = tk.StringVar(value="" if value is None else str(value))
        return var, ttk.Entry(self.form, textvariable=var, width=28)

    # ================================================================== #
    def save_params(self) -> None:
        db = self.app.db
        a = self._algo(self.selected_id) if self.selected_id else None
        if not db or not a:
            return
        errors: list[str] = []
        normalized: dict[str, str] = {}
        for key, (d, var) in self._param_widgets.items():
            raw = var.get()
            if isinstance(raw, bool):
                raw = "1" if raw else "0"
            try:
                normalized[key] = validate(d, raw)
            except ParamError as exc:
                errors.append(str(exc))
        if errors:
            messagebox.showerror("파라미터 오류", "\n".join(errors), parent=self)
            return
        changed = 0
        for key, val in normalized.items():
            try:
                if db.save_param(a["id"], key, val, updated_by="ui"):
                    changed += 1
            except Exception:  # noqa: BLE001
                log.exception("파라미터 저장 실패 %s", key)
        self.status_var.set(f"{changed}개 항목 저장됨")
        if changed:
            self.app.log_event("INFO", "algo", f"파라미터 변경: {a['code']} {changed}건")
        self.reload()

    def restore_defaults(self) -> None:
        a = self._algo(self.selected_id) if self.selected_id else None
        if not a:
            return
        if not messagebox.askyesno("기본값 복원",
                                   f"{a['name']} 의 모든 파라미터를 기본값으로 되돌릴까요?", parent=self):
            return
        db = self.app.db
        changed = 0
        for d in a.get("param_defs") or []:
            try:
                if db.save_param(a["id"], d["param_key"], d["default_value"], updated_by="ui:default"):
                    changed += 1
            except Exception:  # noqa: BLE001
                log.exception("기본값 복원 실패")
        self.status_var.set(f"기본값 복원 {changed}건")
        self.app.log_event("INFO", "algo", f"파라미터 기본값 복원: {a['code']} {changed}건")
        self.reload()

    def save_selection(self) -> None:
        db = self.app.db
        if not db:
            return
        n = 0
        for a in self.algos:
            aid = a["id"]
            enabled = bool(self._enabled_vars[aid].get()) or bool(a["is_locked"])
            try:
                priority = int(self._priority_vars[aid].get())
            except (TypeError, ValueError):
                priority = a["priority"]
            try:
                db.save_selection(aid, enabled, priority, updated_by="ui")
                n += 1
            except Exception:  # noqa: BLE001
                log.exception("선택 저장 실패")
        self.status_var.set(f"선택/우선순위 저장 ({n})")
        self.app.log_event("INFO", "algo", "알고리즘 선택/우선순위 변경")
        self.reload()
