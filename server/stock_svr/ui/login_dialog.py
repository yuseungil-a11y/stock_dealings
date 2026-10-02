"""앱 화면 잠금 해제용 로그인 창.

공용 서버에서 다른 사람이 화면을 그냥 볼 수 없도록, 프로그램 시작 시 / 트레이에서
창을 복원할 때 이 대화상자로 로그인을 요구한다. `app_user`(웹과 공유하는 계정, bcrypt
해시)로 검증하며, `AutoTradeStartDialog`(자동거래 시작 확인)와 달리 **admin 권한을
요구하지 않는다** — "화면을 볼 수 있는가"만 확인하는 게이트이고, 실전 주문 시작의
관리자 재확인은 별도로 그대로 유지된다.
"""
from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk

log = logging.getLogger(__name__)


class LoginDialog(tk.Toplevel):
    """앱 화면 잠금 해제용 로그인 (app_user, 웹과 공유 계정). admin 아니어도 통과."""

    def __init__(self, master, db, title: str = "로그인", use_transient: bool = True):
        """`use_transient=False` 로 호출하면 이 창을 master 의 owner-window 로 묶지 않는다.

        transient(master) 는 master 를 이 창의 owner 로 등록하는데, Windows 에서는 master
        (App)가 **현재 withdraw 상태**이면 이 Toplevel 도 owner 관계 때문에 영원히
        viewable 이 되지 않는다 - 그 결과 grab_set()/wait_visibility() 가 무한 대기하며
        창이 아예 뜨지 않는다(2026-09-30 실제 로그로 확인: "매핑 대기 시작" 이후 그대로
        멈춤, 예외도 없음). 처음에는 "master 가 프로세스 시작 이후 단 한 번도 매핑된 적
        없는 경우"에만 문제가 있고 "한 번 보였다가 나중에 withdraw 된 경우"는 안전할
        것이라 추정했으나, 같은 날 트레이 복원 경로(`App._tray_restore_clicked`, master 가
        로그인 성공 후 한 번 보였다가 "트레이로 이동"으로 withdraw 된 상태)에서도 '열기'
        클릭마다 동일하게 wait_visibility() 무한 대기가 재현되어(4회 연속) 그 추정이
        틀렸음이 확인됐다 - "이전에 보였는지"는 무관하고 **현재 withdraw 상태인지만**이
        문제다. 따라서 시작 로그인 게이트(`App._require_login_or_exit`)와 트레이 복원
        게이트(`App._tray_restore_clicked`) 모두 `use_transient=False` 로 호출한다. 기본값
        True 는 master 가 실제로 보이는(withdraw 되지 않은) 상태에서 다이얼로그를 띄우는
        경우를 위해 남겨둔다.
        """
        log.info("로그인 다이얼로그 생성 시작 (use_transient=%s)", use_transient)
        super().__init__(master)
        self.db = db
        self.result = False
        self.verified_username: str | None = None

        self.title(title)
        self.resizable(False, False)
        if use_transient:
            self.transient(master)

        frm = ttk.Frame(self, padding=16)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="계속하려면 로그인하세요.",
                  font=("맑은 고딕", 11, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        self.user_var = tk.StringVar()
        self.pw_var = tk.StringVar()
        self.error_var = tk.StringVar()

        ttk.Label(frm, text="아이디", width=8).grid(row=1, column=0, sticky="e", pady=3)
        user_entry = ttk.Entry(frm, textvariable=self.user_var, width=24)
        user_entry.grid(row=1, column=1, sticky="w", pady=3)

        ttk.Label(frm, text="비밀번호", width=8).grid(row=2, column=0, sticky="e", pady=3)
        pw_entry = ttk.Entry(frm, textvariable=self.pw_var, width=24, show="*")
        pw_entry.grid(row=2, column=1, sticky="w", pady=3)

        ttk.Label(frm, textvariable=self.error_var, foreground="#c62828").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))

        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="로그인", command=self._ok).pack(side="right")
        ttk.Button(btns, text="취소", command=self._cancel).pack(side="right", padx=6)

        self.bind("<Return>", lambda _e: self._ok())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _e: self._cancel())

        # 부모(App)가 withdraw 상태일 수 있어 화면 기준으로 명시적으로 중앙 배치한다.
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x = (self.winfo_screenwidth() - w) // 2
        y = (self.winfo_screenheight() - h) // 2
        self.geometry(f"+{x}+{y}")

        # grab_set() 은 창이 실제로 화면에 매핑(viewable)된 뒤에만 성공한다. master(App)가
        # 프로세스 시작 이후 단 한 번도 매핑된 적 없이 withdraw 상태였던 경우, 이 Toplevel도
        # update_idletasks() 만으로는 아직 viewable 이 아닐 수 있어 grab_set() 이
        # "TclError: grab failed: window not viewable" 로 실패할 수 있다 - wait_visibility() 로
        # 실제 매핑을 기다린 뒤 grab_set() 을 호출한다.
        log.info("로그인 다이얼로그 매핑 대기 시작 (wait_visibility)")
        self.wait_visibility()
        log.info("로그인 다이얼로그 매핑 완료 - geometry=%s viewable=%s ismapped=%s",
                  self.winfo_geometry(), self.winfo_viewable(), self.winfo_ismapped())
        self.grab_set()
        log.info("로그인 다이얼로그 grab_set 완료")
        user_entry.focus_set()
        log.info("로그인 다이얼로그 생성 완료")

    # ------------------------------------------------------------------ #
    def _ok(self) -> None:
        username = self.user_var.get().strip()
        password = self.pw_var.get()
        if not username or not password:
            self.error_var.set("아이디와 비밀번호를 입력하세요")
            return
        ok, role_or_reason = self.db.verify_login(username, password)
        if not ok:
            log.warning("로그인 실패 (id=%s): %s", username, role_or_reason)
            # 계정 존재 여부·구체적 실패 사유는 화면에 노출하지 않는다(로그에만 남김).
            self.error_var.set("아이디 또는 비밀번호가 올바르지 않습니다")
            self.pw_var.set("")
            return
        self.result = True
        self.verified_username = username
        self.destroy()

    def _cancel(self) -> None:
        self.result = False
        self.destroy()
