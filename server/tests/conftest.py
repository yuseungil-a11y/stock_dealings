"""테스트 공용 픽스처. 실 DB/실 API 를 전혀 쓰지 않는다."""
from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from stock_svr.engine.context import EngineContext, OrderGateState  # noqa: E402
from stock_svr.kiwoom.fake import FakeRest  # noqa: E402


class FakeDb:
    """Executor / risk_guard 가 쓰는 DB 메서드만 흉내내는 가짜 DB."""

    def __init__(self):
        self.orders: list[dict] = []
        self.signals: list[dict] = []
        self.position_states: dict[tuple[int, str], dict] = {}
        self.open_order_codes: set[str] = set()
        self.last_order_times: dict[tuple[str, str], _dt.datetime] = {}
        self.orders_today = 0
        self.new_entries_today = 0
        self.realized_pl = 0
        self.param_values: dict[tuple[str, str], str] = {}
        self.settings: dict[str, str] = {
            "order_enabled": "0", "trading_mode": "real", "real_trading_confirm": "0"}
        self.statuses: dict[str, tuple[str, str]] = {}
        self.algorithms: list[dict] = []
        self.holding_rows: list[dict] = []
        self.balance: dict | None = None
        self.fail_on: set[str] = set()      # 메서드명을 넣으면 해당 조회가 예외를 던진다
        self.llm_decisions: list[dict] = []
        self.events: list[tuple[str, str, str]] = []
        self.bars: dict[str, list[dict]] = {}
        self.stock_states: dict[str, str] = {}
        # universe_filter 용 종목마스터 대역
        self.stock_master_rows: list[dict] = []
        self.stock_master_updated_at: _dt.datetime | None = None
        self.executions: list[dict] = []
        self.order_algos: dict[str, str] = {}
        self.expired_unknown = 0
        self.order_events: list[dict] = []
        self.purged: list[tuple[str, int]] = []      # (대상, 보관일수)
        # 산업 트렌드 스캔 (claude_trend_scan)
        self.trend_runs: list[dict] = []
        self.trend_candidates: list[dict] = []
        self.trend_attempts: list[dict] = []      # append-only 감사로그
        self.trend_requests: list[dict] = []      # 웹의 "지금 다시 조사" 요청 큐
        self.now_for_stale: _dt.datetime | None = None   # 멈춘 요청 정리 기준 시각(테스트)
        # 기업 재무분석 (DART + Claude, 매매 무관)
        self.corp_codes: dict[str, dict] = {}
        self.corp_codes_updated_at: _dt.datetime | None = None
        self.financials: dict[tuple[str, int, str], dict] = {}
        self.valuations: dict[tuple[str, _dt.date], dict] = {}
        self.company_reports: dict[tuple[str, _dt.date], dict] = {}
        self.price_daily: dict[str, list[dict]] = {}
        self._carid = 0
        self._taid = 0
        self._trid = 0
        self._oid = 0
        self._sid = 0
        self._lid = 0
        self._tid = 0
        self._tcid = 0

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail_on:
            raise RuntimeError(f"FakeDb 강제 오류: {name}")

    # -- 설정/상태 ------------------------------------------------------ #
    def get_settings(self) -> dict:
        self._maybe_fail("get_settings")
        return dict(self.settings)

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value, updated_by="test"):
        # R-10: 실제 Database 와 같이 게이트 3키는 set_setting 으로 못 바꾼다
        from stock_svr.db import GATE_KEYS, GateChangeError

        if key in GATE_KEYS:
            raise GateChangeError(f"'{key}' 는 set_gate() 로만 변경할 수 있습니다.")
        self.settings[key] = str(value)

    def set_gate(self, key, value, *, confirm_token, updated_by="test"):
        from stock_svr.db import GATE_CONFIRM_TOKEN, GATE_KEYS, GateChangeError

        if key not in GATE_KEYS:
            raise GateChangeError(f"'{key}' 는 게이트 키가 아닙니다.")
        if confirm_token != GATE_CONFIRM_TOKEN:
            raise GateChangeError("확인 토큰 불일치")
        old = self.settings.get(key)
        if str(old) == str(value):
            return False
        self.settings[key] = str(value)
        self.log_event("WARN", "system", f"주문 게이트 변경: {key} {old!r} → {value!r}")
        return True

    def set_status(self, component, status, message=None):
        self.statuses[component] = (status, message or "")

    def load_algorithms(self):
        self._maybe_fail("load_algorithms")
        return [dict(a) for a in self.algorithms]

    def latest_balance(self, account_id):
        return dict(self.balance) if self.balance else None

    def get_holdings(self, account_id):
        return [dict(h) for h in self.holding_rows]

    def get_position_states(self, account_id):
        return {k[1]: dict(v) for k, v in self.position_states.items() if k[0] == account_id}

    def count_open_orders(self, account_id: int) -> int:
        return len(self.open_order_codes)

    # -- orders -------------------------------------------------------- #
    def insert_order(self, **f):
        self._oid += 1
        row = dict(f)
        row["id"] = self._oid
        self.orders.append(row)
        return self._oid

    def update_order(self, order_id: int, **f):
        for row in self.orders:
            if row["id"] == order_id:
                row.update(f)
                return 1
        return 0

    def find_order_by_ordno(self, account_id: int, ord_no: str):
        self._maybe_fail("find_order_by_ordno")
        for row in reversed(self.orders):
            if str(row.get("ord_no") or "") == str(ord_no):
                return dict(row)
        return None

    def upsert_order_by_ordno(self, account_id: int, ord_no: str, **f):
        """실제 Database 와 같은 동작(있으면 갱신, 없으면 새 행)."""
        existing = self.find_order_by_ordno(account_id, ord_no)
        if existing:
            self.update_order(existing["id"], **f)
            return existing["id"]
        payload = dict(f)
        payload.setdefault("side", "BUY")
        payload.setdefault("status", "ACCEPTED")
        payload["account_id"] = account_id
        payload["ord_no"] = ord_no
        return self.insert_order(**payload)

    def insert_order_event(self, **f):
        self._maybe_fail("insert_order_event")
        row = dict(f)
        row["id"] = len(self.order_events) + 1
        self.order_events.append(row)
        return row["id"]

    def order_events_of(self, order_id: int) -> list[dict]:
        return [e for e in self.order_events if e.get("order_id") == order_id]

    def has_open_order(self, account_id: int, stk_cd: str, side: str | None = None) -> bool:
        self._maybe_fail("has_open_order")
        if side and (stk_cd, side) in self.open_order_codes:
            return True
        return stk_cd in self.open_order_codes

    def expire_unknown_sent_orders(self, account_id: int, minutes: int = 10) -> int:
        self._maybe_fail("expire_unknown_sent_orders")
        n = 0
        for row in self.orders:
            if row.get("ord_no") is None and row.get("status") == "SENT" \
                    and row.get("is_dry_run") == 0:
                row["status"] = "FAILED"
                row["return_msg"] = "UNKNOWN 확정 불가"
                n += 1
        self.expired_unknown += n
        return n

    def upsert_execution(self, account_id, ord_no, cntr_no, stk_cd, stk_nm, side,
                         cntr_qty, cntr_pric, executed_at, cmsn=None, tax=None,
                         source="WS", only_if_absent=False):
        self._maybe_fail("upsert_execution")
        key = (str(ord_no), str(cntr_no))
        for e in self.executions:
            if (str(e.get("ord_no")), str(e.get("cntr_no"))) == key:
                if only_if_absent:
                    return
                e.update({"cntr_qty": cntr_qty, "cntr_pric": cntr_pric,
                          "executed_at": executed_at})
                return
        if only_if_absent and any(e.get("ord_no") == ord_no and e.get("cntr_qty") == cntr_qty
                                  and e.get("cntr_pric") == cntr_pric for e in self.executions):
            return
        self.executions.append({
            "account_id": account_id, "ord_no": ord_no, "cntr_no": cntr_no, "stk_cd": stk_cd,
            "stk_nm": stk_nm, "side": side, "cntr_qty": cntr_qty, "cntr_pric": cntr_pric,
            "executed_at": executed_at, "cmsn": cmsn, "tax": tax, "source": source})

    def reduce_position_invest(self, account_id, stk_cd, amount):
        self._maybe_fail("reduce_position_invest")
        st = self.position_states.get((account_id, stk_cd))
        if st:
            st["total_invested"] = max(0, int(st.get("total_invested") or 0) - int(amount))

    def count_executions_since(self, account_id: int, stk_cd: str, since) -> int:
        self._maybe_fail("count_executions_since")
        return sum(1 for e in self.executions
                   if e.get("stk_cd") == stk_cd and e.get("executed_at") >= since)

    def order_algo_code(self, account_id: int, ord_no: str):
        self._maybe_fail("order_algo_code")
        return self.order_algos.get(str(ord_no))

    def stock_state(self, stk_cd: str):
        self._maybe_fail("stock_state")
        return self.stock_states.get(stk_cd)

    def last_order_at(self, account_id: int, stk_cd: str, side: str | None = None):
        self._maybe_fail("last_order_at")
        return self.last_order_times.get((stk_cd, side or "BUY"))

    def count_orders_today(self, account_id: int, only_sent: bool = True) -> int:
        self._maybe_fail("count_orders_today")
        return self.orders_today

    def count_new_entries_today(self, account_id: int, algo_code: str | None = None,
                                only_sent: bool = True) -> int:
        return self.new_entries_today

    def today_realized_pl(self, account_id: int, base_dt) -> int:
        self._maybe_fail("today_realized_pl")
        return self.realized_pl

    # -- signal / position --------------------------------------------- #
    def insert_signal(self, run_id, algo_code, stk_cd, stk_nm, signal_type,
                      score=None, detail=None, order_id=None):
        self._sid += 1
        self.signals.append({
            "id": self._sid, "run_id": run_id, "algo_code": algo_code, "stk_cd": stk_cd,
            "stk_nm": stk_nm, "signal_type": signal_type, "score": score,
            "detail": detail, "order_id": order_id})
        return self._sid

    def bump_position_invest(self, account_id, stk_cd, amount, algo_code, price, avg_down):
        self._maybe_fail("bump_position_invest")
        st = self.position_states.setdefault((account_id, stk_cd), {
            "entry_algo": algo_code, "avg_down_count": 0, "total_invested": 0,
            "last_buy_price": None, "stopped": 0})
        st["total_invested"] += int(amount)
        st["last_buy_price"] = price
        if avg_down:
            st["avg_down_count"] += 1

    def upsert_position_state(self, account_id, stk_cd, **fields):
        st = self.position_states.setdefault((account_id, stk_cd), {
            "entry_algo": None, "avg_down_count": 0, "total_invested": 0,
            "last_buy_price": None, "stopped": 0})
        st.update(fields)

    # -- Claude 거부권 필터 --------------------------------------------- #
    def insert_llm_decision(self, **f):
        self._maybe_fail("insert_llm_decision")
        self._lid += 1
        row = dict(f)
        row["id"] = self._lid
        self.llm_decisions.append(row)
        return self._lid

    def llm_usage_today(self) -> dict:
        self._maybe_fail("llm_usage_today")
        real = [r for r in self.llm_decisions if not r.get("from_cache")]
        return {
            "calls": len(real),
            "input_tokens": sum(int(r.get("input_tokens") or 0) for r in real),
            "output_tokens": sum(int(r.get("output_tokens") or 0) for r in real),
        }

    def recent_bars(self, stk_cd: str, limit: int = 30):
        return list(self.bars.get(stk_cd, []))[-limit:]

    # -- 종목마스터 (universe_filter) ----------------------------------- #
    def stock_master_stats(self) -> dict:
        self._maybe_fail("stock_master_stats")
        return {"count": len(self.stock_master_rows),
                "updated_at": self.stock_master_updated_at}

    def stock_master_universe(self, market_codes=("0", "10")) -> list[dict]:
        self._maybe_fail("stock_master_universe")
        wanted = {str(c) for c in market_codes}
        return [dict(r) for r in self.stock_master_rows
                if str(r.get("market_code")) in wanted]

    # -- 산업 트렌드 스캔 (claude_trend_scan) --------------------------- #
    def find_stocks_by_name(self, stk_nm: str) -> list[dict]:
        """실제 Database 와 같이 **공백 제거 후 정확 일치**, 코스피/코스닥만."""
        self._maybe_fail("find_stocks_by_name")
        name = "".join(str(stk_nm or "").split())
        if not name:
            return []
        return [dict(r) for r in self.stock_master_rows
                if "".join(str(r.get("stk_nm") or "").split()) == name
                and str(r.get("market_code")) in ("0", "10")]

    def trend_scan_run_on(self, scan_date):
        self._maybe_fail("trend_scan_run_on")
        for r in self.trend_runs:
            if r["scan_date"] == scan_date:
                return dict(r)
        return None

    def start_trend_scan_run(self, scan_date, region_scope, model):
        self._maybe_fail("start_trend_scan_run")
        if any(r["scan_date"] == scan_date for r in self.trend_runs):
            # 실제 DB 의 UNIQUE(scan_date) 위반 대역
            raise RuntimeError("Duplicate entry for key 'uq_trend_scan_date'")
        self._tid += 1
        self.trend_runs.append({"id": self._tid, "scan_date": scan_date, "status": "ok",
                                "region_scope": region_scope, "model": model,
                                "candidate_count": 0, "signal_count": 0})
        return self._tid

    def finish_trend_scan_run(self, run_id, **f):
        self._maybe_fail("finish_trend_scan_run")
        from stock_svr.db import TREND_TEXT_MAX, _trim_masked

        for r in self.trend_runs:
            if r["id"] == run_id:
                row = dict(f)
                for key, limit in (("domestic_theme_summary", TREND_TEXT_MAX),
                                   ("research_summary", TREND_TEXT_MAX),
                                   ("error_msg", 255)):
                    if key in row:
                        row[key] = _trim_masked(row[key], limit)
                r.update(row)
                return 1
        return 0

    def insert_trend_candidate(self, run_id, **f):
        self._maybe_fail("insert_trend_candidate")
        self._tcid += 1
        row = dict(f)
        row["id"] = self._tcid
        row["run_id"] = run_id
        row.setdefault("signal_id", None)
        self.trend_candidates.append(row)
        return self._tcid

    def set_trend_candidate_signal(self, candidate_id, signal_id):
        self._maybe_fail("set_trend_candidate_signal")
        for r in self.trend_candidates:
            if r["id"] == candidate_id:
                r["signal_id"] = signal_id
                return 1
        return 0

    def trend_scan_candidates(self, run_id):
        return [dict(r) for r in self.trend_candidates if r.get("run_id") == run_id]

    # -- 시도 감사로그 (append-only) ------------------------------------ #
    def insert_trend_scan_attempt(self, scan_date, **f):
        self._maybe_fail("insert_trend_scan_attempt")
        from stock_svr.db import TREND_TEXT_MAX, _trim_masked

        self._taid += 1
        row = dict(f)
        row["id"] = self._taid
        row["scan_date"] = scan_date
        # 실제 테이블에는 항상 있는 컬럼들(값이 없으면 NULL)
        for key in ("requested_by", "region_scope", "model", "candidate_count",
                    "web_search_count", "input_tokens", "output_tokens", "latency_ms",
                    "error_msg", "research_summary", "started_at", "finished_at"):
            row.setdefault(key, None)
        for key, limit in (("research_summary", TREND_TEXT_MAX), ("error_msg", 255),
                           ("requested_by", 50)):
            if row.get(key) is not None:
                row[key] = _trim_masked(row[key], limit)
        self.trend_attempts.append(row)
        return self._taid

    def trend_scan_attempts(self, scan_date):
        return [dict(r) for r in self.trend_attempts if r.get("scan_date") == scan_date]

    # -- 수동 재조사 결과 반영 (run UPSERT + 후보 교체) ------------------ #
    def save_manual_trend_scan(self, scan_date, *, requested_by, status, region_scope,
                               model, candidates, domestic_theme_summary=None,
                               research_summary=None, web_search_count=None,
                               input_tokens=None, output_tokens=None, latency_ms=None,
                               error_msg=None, started_at=None):
        self._maybe_fail("save_manual_trend_scan")
        from stock_svr.db import TREND_TEXT_MAX, _trim_masked

        row = None
        for r in self.trend_runs:
            if r["scan_date"] == scan_date:
                row = r
                break
        if row is None:
            self._tid += 1
            row = {"id": self._tid, "scan_date": scan_date}
            self.trend_runs.append(row)
        row.update({
            "trigger_type": "manual", "requested_by": _trim_masked(requested_by, 50),
            "started_at": started_at, "status": status, "region_scope": region_scope,
            "model": model, "candidate_count": len(candidates), "signal_count": 0,
            "domestic_theme_summary": _trim_masked(domestic_theme_summary, TREND_TEXT_MAX),
            "research_summary": _trim_masked(research_summary, TREND_TEXT_MAX),
            "web_search_count": web_search_count, "input_tokens": input_tokens,
            "output_tokens": output_tokens, "latency_ms": latency_ms,
            "error_msg": _trim_masked(error_msg, 255)})
        run_id = int(row["id"])
        # 기존 후보 삭제 후 재삽입 (실제 DB 는 한 트랜잭션)
        self.trend_candidates = [c for c in self.trend_candidates
                                 if c.get("run_id") != run_id]
        for c in candidates:
            self._tcid += 1
            new = dict(c)
            new["id"] = self._tcid
            new["run_id"] = run_id
            new["signal_id"] = None
            self.trend_candidates.append(new)
        return run_id

    # -- 수동 재조사 요청 큐 -------------------------------------------- #
    def add_trend_scan_request(self, requested_by="admin", requested_at=None):
        """테스트 헬퍼 — 웹이 INSERT 하는 pending 행 흉내."""
        self._trid += 1
        row = {"id": self._trid, "requested_by": requested_by, "status": "pending",
               "requested_at": requested_at or _dt.datetime(2026, 9, 18, 8, 40),
               "run_id": None, "error_msg": None, "processed_at": None}
        self.trend_requests.append(row)
        return row

    def pending_trend_scan_request(self):
        self._maybe_fail("pending_trend_scan_request")
        rows = [r for r in self.trend_requests if r["status"] == "pending"]
        rows.sort(key=lambda r: (r["requested_at"], r["id"]))
        return dict(rows[0]) if rows else None

    def count_processing_trend_requests(self) -> int:
        self._maybe_fail("count_processing_trend_requests")
        return sum(1 for r in self.trend_requests if r["status"] == "processing")

    def claim_trend_scan_request(self, max_tries: int = 5):
        """실제 Database 와 같은 '영향 행 수로 승자 판정' 방식."""
        self._maybe_fail("claim_trend_scan_request")
        for _ in range(max(1, int(max_tries))):
            row = self.pending_trend_scan_request()
            if row is None:
                return None
            won = 0
            for r in self.trend_requests:
                if r["id"] == row["id"] and r["status"] == "pending":
                    r["status"] = "processing"
                    won = 1
            if won:
                out = dict(row)
                out["status"] = "processing"
                return out
        return None

    def finish_trend_scan_request(self, request_id, *, status, run_id=None,
                                  error_msg=None) -> int:
        self._maybe_fail("finish_trend_scan_request")
        from stock_svr.db import _trim_masked

        for r in self.trend_requests:
            if r["id"] == request_id:
                r.update({"status": status, "run_id": run_id,
                          "error_msg": _trim_masked(error_msg, 255),
                          "processed_at": _dt.datetime(2026, 9, 18, 9, 0)})
                return 1
        return 0

    def expire_stale_trend_requests(self, minutes=10, reason="서버 재시작으로 중단") -> int:
        self._maybe_fail("expire_stale_trend_requests")
        cutoff = (self.now_for_stale or _dt.datetime(2026, 9, 18, 8, 40)) \
            - _dt.timedelta(minutes=max(1, int(minutes)))
        n = 0
        for r in self.trend_requests:
            if r["status"] == "processing" and r["requested_at"] < cutoff:
                r.update({"status": "error", "error_msg": reason,
                          "processed_at": cutoff})
                n += 1
        return n

    # -- 기업 재무분석 (DART + Claude, 매매 무관) ------------------------ #
    def company_corp_code_stats(self) -> dict:
        self._maybe_fail("company_corp_code_stats")
        return {"count": len(self.corp_codes), "updated_at": self.corp_codes_updated_at}

    def upsert_company_corp_codes(self, rows) -> int:
        self._maybe_fail("upsert_company_corp_codes")
        n = 0
        for r in rows:
            if not r.get("stk_cd") or not r.get("corp_code"):
                continue
            self.corp_codes[str(r["stk_cd"])] = {
                "stk_cd": str(r["stk_cd"]), "corp_code": str(r["corp_code"])[:8],
                "corp_name": str(r.get("corp_name") or "")[:120]}
            n += 1
        return n

    def company_corp_codes(self, stk_cds=None):
        self._maybe_fail("company_corp_codes")
        if stk_cds is None:
            return [dict(v) for _, v in sorted(self.corp_codes.items())]
        wanted = {str(c) for c in stk_cds if c}
        return [dict(v) for k, v in sorted(self.corp_codes.items()) if k in wanted]

    def company_financial_keys(self, stk_cds):
        self._maybe_fail("company_financial_keys")
        wanted = {str(c) for c in stk_cds if c}
        return {k for k in self.financials if k[0] in wanted}

    def upsert_company_financial(self, stk_cd, bsns_year, reprt_code, **values) -> int:
        self._maybe_fail("upsert_company_financial")
        from stock_svr.db import Database

        key = (str(stk_cd), int(bsns_year), str(reprt_code))
        row = {"stk_cd": key[0], "bsns_year": key[1], "reprt_code": key[2]}
        for col in Database._FINANCIAL_COLS:      # noqa: SLF001 - 실제 컬럼 목록과 맞춘다
            value = values.get(col)
            row[col] = None if value is None else int(value)
        self.financials[key] = row
        return 1

    def company_financials(self, stk_cd, since_year=None):
        self._maybe_fail("company_financials")
        rows = [dict(v) for k, v in self.financials.items()
                if k[0] == str(stk_cd) and (since_year is None or k[1] >= int(since_year))]
        rows.sort(key=lambda r: (r["bsns_year"], r["reprt_code"]))
        return rows

    def upsert_company_valuation(self, stk_cd, dt, **values) -> int:
        self._maybe_fail("upsert_company_valuation")
        row = {"stk_cd": str(stk_cd), "dt": dt}
        row.update({k: values.get(k) for k in
                    ("cur_prc", "eps_ttm", "bps", "per", "pbr", "roe", "debt_ratio",
                     "financial_asof")})
        self.valuations[(str(stk_cd), dt)] = row
        return 1

    def company_valuation(self, stk_cd, dt):
        row = self.valuations.get((str(stk_cd), dt))
        return dict(row) if row else None

    def latest_company_valuation(self, stk_cd):
        self._maybe_fail("latest_company_valuation")
        rows = [v for (code, _dt_), v in self.valuations.items() if code == str(stk_cd)]
        if not rows:
            return None
        rows.sort(key=lambda r: r["dt"])
        return dict(rows[-1])

    def upsert_company_report(self, stk_cd, as_of_date, *, model, report_text, stk_nm=None,
                              summary=None, input_tokens=None, output_tokens=None,
                              latency_ms=None, status="ok", error_msg=None) -> int:
        self._maybe_fail("upsert_company_report")
        from stock_svr.db import COMPANY_REPORT_TEXT_MAX, _trim_masked

        key = (str(stk_cd), as_of_date)
        existing = self.company_reports.get(key)
        if existing is None:
            self._carid += 1
        self.company_reports[key] = {
            "id": existing["id"] if existing else self._carid,
            "stk_cd": key[0], "as_of_date": as_of_date,
            "stk_nm": _trim_masked(stk_nm, 60), "model": str(model)[:50],
            "summary": _trim_masked(summary, 500),
            "report_text": _trim_masked(report_text, COMPANY_REPORT_TEXT_MAX) or "-",
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "latency_ms": latency_ms,
            "status": status if status in ("ok", "error") else "error",
            "error_msg": _trim_masked(error_msg, 255)}
        return 1

    def company_report_on(self, stk_cd, as_of_date):
        row = self.company_reports.get((str(stk_cd), as_of_date))
        return dict(row) if row else None

    def company_reported_codes(self, as_of_date) -> set:
        self._maybe_fail("company_reported_codes")
        return {k[0] for k, v in self.company_reports.items()
                if k[1] == as_of_date and v.get("status") == "ok"}

    def latest_closes(self, stk_cds) -> dict:
        self._maybe_fail("latest_closes")
        out = {}
        for code in {str(c) for c in stk_cds if c}:
            bars = sorted(self.price_daily.get(code, []), key=lambda b: b["dt"])
            if bars and bars[-1].get("cur_prc"):
                out[code] = int(bars[-1]["cur_prc"])
        return out

    # -- 기타 ---------------------------------------------------------- #
    def scalar(self, sql: str, args=None, default=None):
        if "stop_loss_pct" in sql:
            return self.param_values.get(("risk_guard", "stop_loss_pct"), "-15")
        return default

    def query(self, sql: str, args=None):
        return []

    def log_event(self, level, category, message):
        self.events.append((level, category, message))
        return len(self.events)

    # -- 보관 정책 ------------------------------------------------------ #
    def purge_old(self, retention_days: int = 7) -> dict:
        self._maybe_fail("purge_old")
        days = max(1, int(retention_days))
        self.purged += [("event_log", days), ("api_call_log", days),
                        ("screening_result", days * 4)]
        return {"event_log": 0, "api_call_log": 0, "screening_result": 0}

    def purge_archives(self, days=None) -> dict:
        self._maybe_fail("purge_archives")
        from stock_svr.db import ARCHIVE_RETENTION_DEFAULT, ARCHIVE_RETENTION_MIN

        if days is None:
            try:
                days = int(self.settings.get("archive_retention_days",
                                             ARCHIVE_RETENTION_DEFAULT))
            except (TypeError, ValueError):
                days = ARCHIVE_RETENTION_DEFAULT
        days = max(ARCHIVE_RETENTION_MIN, int(days))
        self.purged += [("event_archive", days), ("api_error_log", days)]
        return {"event_archive": 0, "api_error_log": 0}

    # -- 검사 헬퍼 ------------------------------------------------------ #
    @property
    def dry_run_orders(self):
        return [o for o in self.orders if o.get("is_dry_run") == 1]

    @property
    def sent_orders(self):
        return [o for o in self.orders if o.get("is_dry_run") == 0]


class FakeMarket:
    """MarketService 대역."""

    def __init__(self, ranks=None, surges=None, bars=None, quotes=None,
                 themes=None, theme_members=None):
        self._ranks = ranks or []
        self._surges = surges or []
        self._bars = bars or {}
        self._quotes = quotes or {}
        self._themes = themes or []
        self._theme_members = theme_members or {}
        self.recorded: list[tuple[str, list]] = []
        self.theme_calls: list[str] = []
        self.fail_on: set[str] = set()

    def rank_flu_rt(self, mrkt_tp="000", stex_tp="1"):
        return list(self._ranks)

    def volume_surge(self, mrkt_tp="000", stex_tp="1"):
        return list(self._surges)

    def record_screening(self, source_api, items):
        self.recorded.append((source_api, list(items)))
        return len(items)

    def daily_bars(self, stk_cd, base_dt=None, store=True):
        return list(self._bars.get(stk_cd, []))

    def quote(self, stk_cd):
        return self._quotes.get(stk_cd)

    # -- 테마 (ka90001 / ka90002) --------------------------------------- #
    def themes(self, flu_pl_amt_tp="3", stex_tp="1", date_tp="10"):
        if "themes" in self.fail_on:
            raise RuntimeError("FakeMarket 강제 오류: themes")
        self.theme_calls.append("ka90001")
        return list(self._themes)

    def theme_members(self, thema_grp_cd, stex_tp="1", date_tp="2"):
        if "theme_members" in self.fail_on:
            raise RuntimeError("FakeMarket 강제 오류: theme_members")
        self.theme_calls.append(f"ka90002:{thema_grp_cd}")
        return list(self._theme_members.get(str(thema_grp_cd), []))


@pytest.fixture(autouse=True)
def _reset_untradable_log():
    """'거래불가 종목 제외' 하루 1회 로그 표시가 테스트 간에 새지 않게 한다 (R-06)."""
    from stock_svr.engine.context import reset_untradable_log_state

    reset_untradable_log_state()
    yield
    reset_untradable_log_state()


@pytest.fixture(autouse=True)
def _reset_universe_cache():
    """universe_filter 의 시총 순위 캐시가 테스트 간에 새지 않게 한다."""
    from stock_svr.algo.universe_filter import clear_universe_cache

    clear_universe_cache()
    yield
    clear_universe_cache()


@pytest.fixture(autouse=True)
def _reset_trend_scan():
    """'오늘 이미 조사함' 표시가 테스트 간에 새지 않게 한다 (claude_trend_scan)."""
    from stock_svr.services.trend_scan import reset_state

    reset_state()
    yield
    reset_state()


@pytest.fixture(autouse=True)
def _reset_fundamentals():
    """기업 재무분석의 날짜 캐시(하루 1회 / corpCode 하루 1회)가 테스트 간에 새지 않게 한다."""
    from stock_svr.services.corp_code_sync import reset_state as reset_corp
    from stock_svr.services.fundamentals import reset_state as reset_fund

    reset_fund()
    reset_corp()
    yield
    reset_fund()
    reset_corp()


@pytest.fixture
def fake_db():
    return FakeDb()


@pytest.fixture
def fake_rest():
    return FakeRest()


def make_ctx(db=None, *, order_enabled=False, trading_mode="real", real_confirm=False,
             holdings=None, positions=None, balance=None, market=None,
             market_open=True, now=None, account_id=1, run_id=1, cooldown_sec=0,
             anthropic_cfg=None):
    """테스트용 EngineContext 조립."""
    settings = {
        "order_enabled": "1" if order_enabled else "0",
        "trading_mode": trading_mode,
        "real_trading_confirm": "1" if real_confirm else "0",
    }
    now = now or _dt.datetime(2026, 9, 18, 10, 30, 0)   # 금요일 장중
    db = db if db is not None else FakeDb()
    if isinstance(db, FakeDb):
        db.settings = dict(settings)          # 게이트 DB 재확인(S-24)이 같은 값을 보도록
    if balance is None:
        balance = {"entr": 10_000_000, "ord_alow_amt": 10_000_000,
                   "prsm_dpst_aset_amt": 10_000_000, "snapshot_at": now}
    balance.setdefault("snapshot_at", now)
    return EngineContext(
        db=db,
        account_id=account_id,
        settings=settings,
        gate=OrderGateState.from_settings(settings),
        market=market,
        anthropic_cfg=anthropic_cfg,
        run_id=run_id,
        now=now,
        holdings=holdings or {},
        position_states=positions or {},
        balance=balance,
        market_open=market_open,
        cooldown_sec=cooldown_sec,
    )


@pytest.fixture
def ctx_factory():
    return make_ctx
