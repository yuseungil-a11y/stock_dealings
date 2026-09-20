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
        self.executions: list[dict] = []
        self.order_algos: dict[str, str] = {}
        self.expired_unknown = 0
        self._oid = 0
        self._sid = 0
        self._lid = 0

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

    # -- 검사 헬퍼 ------------------------------------------------------ #
    @property
    def dry_run_orders(self):
        return [o for o in self.orders if o.get("is_dry_run") == 1]

    @property
    def sent_orders(self):
        return [o for o in self.orders if o.get("is_dry_run") == 0]


class FakeMarket:
    """MarketService 대역."""

    def __init__(self, ranks=None, surges=None, bars=None, quotes=None):
        self._ranks = ranks or []
        self._surges = surges or []
        self._bars = bars or {}
        self._quotes = quotes or {}
        self.recorded: list[tuple[str, list]] = []

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


@pytest.fixture(autouse=True)
def _reset_untradable_log():
    """'거래불가 종목 제외' 하루 1회 로그 표시가 테스트 간에 새지 않게 한다 (R-06)."""
    from stock_svr.engine.context import reset_untradable_log_state

    reset_untradable_log_state()
    yield
    reset_untradable_log_state()


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
