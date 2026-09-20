"""보안 재감사(Spinoza) R-01~R-18 회귀 테스트.

실서버 주문 API·Anthropic API 는 어떤 경우에도 호출하지 않는다(전부 fake/스텁).
"""
from __future__ import annotations

import datetime as _dt
import os
import subprocess
import sys
import textwrap

import pytest

from conftest import SERVER_DIR, FakeDb, FakeMarket, make_ctx
from stock_svr import single_instance as si
from stock_svr.algo.base import KIND_LIQUIDATE, KIND_STOP_LOSS, Signal, amount_with_buffer
from stock_svr.algo.params import ParamSet
from stock_svr.algo.registry import build as build_algo
from stock_svr.algo.risk_guard import RiskGuard
from stock_svr.config import AppConfig, DbConfig
from stock_svr.db import GATE_CONFIRM_TOKEN, GATE_KEYS, Database, GateChangeError
from stock_svr.engine.context import OrderGateState, gate_widened
from stock_svr.engine.executor import (
    MAX_SELL_FAILURES,
    PENDING_UNKNOWN_MIN_SYNCS,
    Executor,
)
from stock_svr.engine.runner import NO_GUARD_REASON, Engine, annotate_open_orders
from stock_svr.kiwoom.errors import KiwoomHttpError
from stock_svr.kiwoom.fake import FakeRest
from stock_svr.util import mask_text
from tests_support import RISK_DEFS, buy_signal

from test_auto_trading import MOM_DEFS, risk_guard_meta


# ====================================================================== #
# 공용 헬퍼
# ====================================================================== #
def guard(**over) -> RiskGuard:
    return RiskGuard(meta={"code": "risk_guard", "role": "risk"},
                     params=ParamSet(RISK_DEFS, over))


def sell_signal(stk: str = "005930", qty: int = 10, kind: str = KIND_STOP_LOSS) -> Signal:
    return Signal(algo_code="risk_guard", stk_cd=stk, stk_nm="테스트종목", side="SELL",
                  qty=qty, price=None, trde_tp="3", kind=kind, reason="손절")


def holding(stk_cd="005930", name="테스트종목", qty=10, cur=10_000, **extra) -> dict:
    row = {"stk_cd": stk_cd, "stk_nm": name, "rmnd_qty": qty, "trde_able_qty": qty,
           "cur_prc": cur, "pur_pric": 20_000, "pur_amt": 20_000 * qty,
           "evltv_prft": (cur - 20_000) * qty}
    row.update(extra)
    return row


def engine_with(db: FakeDb, rest: FakeRest, algorithms: list[dict]) -> Engine:
    """DB/네트워크 없이 run_cycle 만 도는 엔진."""
    eng = Engine(AppConfig(), db)
    eng.account_id = 1
    eng.run_id = 7
    eng.market = FakeMarket(
        ranks=[{"rank_no": 1, "stk_cd": "005930", "stk_nm": "삼성전자", "cur_prc": 10000,
                "flu_rt": 5, "now_trde_qty": 100000, "sdnin_rt": None}],
        surges=[{"rank_no": 1, "stk_cd": "005930", "stk_nm": "삼성전자", "cur_prc": 10000,
                 "flu_rt": 5, "now_trde_qty": 100000, "sdnin_rt": 300}])
    eng.executor = Executor(db, rest, 1, 7,
                            auto_trading_check=lambda: eng.auto_trading_active,
                            on_critical=lambda r: None)
    db.algorithms = algorithms
    db.balance = {"entr": 10_000_000, "ord_alow_amt": 10_000_000,
                  "prsm_dpst_aset_amt": 10_000_000, "snapshot_at": _dt.datetime.now()}
    db.settings = {"order_enabled": "1", "trading_mode": "mock", "real_trading_confirm": "0"}
    return eng


def momentum_meta() -> dict:
    return {"id": 2, "code": "momentum_screen", "name": "모멘텀", "role": "entry",
            "is_enabled": 1, "is_locked": 0, "priority": 10, "param_defs": MOM_DEFS,
            "params": {d["param_key"]: d["default_value"] for d in MOM_DEFS}}


class SqlSpyDb(Database):
    """SQL 문자열만 확인하는 Database 대역(실 DB 연결 없음)."""

    def __init__(self):
        Database.__init__(self, DbConfig())
        self.sql: list[tuple[str, tuple]] = []
        self.values: dict[str, str] = {}
        self.events: list[tuple[str, str, str]] = []

    def execute(self, sql, args=None):
        self.sql.append((sql, tuple(args or ())))
        if "system_setting" in sql and args:
            self.values[args[0]] = args[1]
        return 1

    def insert(self, sql, args=None):
        self.sql.append((sql, tuple(args or ())))
        if "event_log" in sql and args:
            self.events.append((args[0], args[1], args[2]))
        return 1

    def query_one(self, sql, args=None):
        self.sql.append((sql, tuple(args or ())))
        if "system_setting" in sql and args:
            key = args[0]
            return {"value": self.values[key]} if key in self.values else None
        return {"n": 0}

    def query(self, sql, args=None):
        self.sql.append((sql, tuple(args or ())))
        return []


# ====================================================================== #
# R-01 : risk_guard 가 없으면 사이클 중단 (fail-open 제거)
# ====================================================================== #
@pytest.mark.parametrize("bad", [
    {"stop_loss_pct": "0"},
    {"max_orders_per_day": "0"},
    {"max_total_invest_pct": "0"},
])
def test_out_of_range_risk_params_block_all_orders(bad):
    """범위 밖 파라미터로 risk_guard 가 빠지면 주문은 0건이어야 한다."""
    defs = RISK_DEFS + [
        {"param_key": "stop_loss_pct", "label": "손절", "value_type": "decimal",
         "default_value": "-15", "min_value": "-90", "max_value": "-0.1"},
        {"param_key": "max_orders_per_day", "label": "일 주문", "value_type": "int",
         "default_value": "30", "min_value": "1", "max_value": "500"},
        {"param_key": "max_total_invest_pct", "label": "총 비중", "value_type": "decimal",
         "default_value": "100", "min_value": "0.1", "max_value": "100"},
    ]
    values = {d["param_key"]: d["default_value"] for d in defs}
    values.update(bad)
    meta = {"id": 1, "code": "risk_guard", "name": "가드", "role": "risk",
            "is_enabled": 1, "is_locked": 1, "priority": 1,
            "param_defs": defs, "params": values}

    assert build_algo(meta) is None            # S-16: 파라미터 오류 → 비활성화

    db, rest = FakeDb(), FakeRest()
    eng = engine_with(db, rest, [meta, momentum_meta()])
    eng.start_auto_trading(by="테스트")
    result = eng.run_cycle(force_market=True)

    assert result["sent"] == 0
    assert result["skipped"] == NO_GUARD_REASON
    assert result["signals"] == []
    assert rest.order_call_count == 0
    assert db.orders == []                     # 신호조차 Executor 로 가지 않는다
    assert any(lvl == "ERROR" for lvl, _, _ in db.events)


def test_missing_guard_raises_error_event_and_status():
    db, rest = FakeDb(), FakeRest()
    eng = engine_with(db, rest, [momentum_meta()])      # risk_guard 아예 없음
    eng.start_auto_trading(by="테스트")
    result = eng.run_cycle(force_market=True)
    assert result["sent"] == 0 and result["skipped"] == NO_GUARD_REASON
    assert db.statuses["auto_trading"][0] == "error"
    assert any(NO_GUARD_REASON in msg for _, _, msg in db.events)


def test_locked_algo_param_error_is_surfaced():
    """is_locked / role=risk 의 파라미터 오류는 조용히 None 이 되지 않는다 (R-01)."""
    seen = []
    meta = {"code": "risk_guard", "name": "가드", "role": "risk", "is_locked": 1,
            "param_defs": [{"param_key": "max_total_invest", "label": "총한도",
                            "value_type": "int", "default_value": "1000000",
                            "min_value": "1"}],
            "params": {"max_total_invest": "0"}}
    assert build_algo(meta, on_error=lambda c, d, crit: seen.append((c, d, crit))) is None
    assert seen and seen[0][0] == "risk_guard" and seen[0][2] is True


def test_normal_algo_param_error_is_not_critical():
    seen = []
    meta = {"code": "momentum_screen", "name": "모멘텀", "role": "entry",
            "param_defs": [{"param_key": "top_n", "label": "상위", "value_type": "int",
                            "default_value": "5", "min_value": "1"}],
            "params": {"top_n": "0"}}
    assert build_algo(meta, on_error=lambda c, d, crit: seen.append((c, d, crit))) is None
    assert seen and seen[0][2] is False


# ====================================================================== #
# R-02 : --smoke 는 게이트가 열려 있으면 자동거래를 시작하지 않는다
# ====================================================================== #
def test_smoke_auto_approval_refuses_when_gate_open():
    from stock_svr.ui.app import smoke_auto_approval

    allowed, why = smoke_auto_approval(gate_open=True)
    assert allowed is False and "실주문" in why
    assert smoke_auto_approval(gate_open=False)[0] is True


class _StubApp:
    """App._start_auto_trading 만 떼어 검증하기 위한 최소 대역(Tk 창 없음)."""

    def __init__(self, db, engine, auto_confirm=True):
        self.db = db
        self.engine = engine
        self._auto_confirm = auto_confirm
        self.logs: list[tuple] = []
        self._account_id = engine.account_id

    def account_id(self):
        """실제 App.account_id 와 동일하게 '메서드'(값이 아니라). 함수 자체를 넘기는 버그를 잡기 위함."""
        return self._account_id

    def log_event(self, level, category, message):
        self.logs.append((level, category, message))

    def _refresh_status(self):
        pass


def test_smoke_does_not_start_auto_trading_with_open_gate():
    from stock_svr.ui.app import App

    db, rest = FakeDb(), FakeRest()
    eng = engine_with(db, rest, [risk_guard_meta(), momentum_meta()])
    db.settings["order_enabled"] = "1"
    db.settings["trading_mode"] = "mock"        # 게이트 ON
    assert eng.current_gate().can_send_order is True

    eng._thread = type("T", (), {"is_alive": lambda self: True})()   # noqa: SLF001
    app = _StubApp(db, eng)
    App._start_auto_trading(app)                                     # noqa: SLF001

    assert eng.auto_trading_active is False        # 시작하지 않음 → 실주문 불가
    assert any("건너뜀" in m for _, _, m in app.logs)


def test_smoke_starts_when_gate_closed():
    from stock_svr.ui.app import App

    db, rest = FakeDb(), FakeRest()
    eng = engine_with(db, rest, [risk_guard_meta(), momentum_meta()])
    db.settings["order_enabled"] = "0"             # 게이트 닫힘 → 관찰 전용
    eng._thread = type("T", (), {"is_alive": lambda self: True})()   # noqa: SLF001
    App._start_auto_trading(_StubApp(db, eng))                       # noqa: SLF001
    assert eng.auto_trading_active is True


# ====================================================================== #
# R-03 : 접수여부 불명은 '증거'로만 해제된다
# ====================================================================== #
def _unknown_executor(now):
    db, rest = FakeDb(), FakeRest()
    rest.raise_on["kt10000"] = KiwoomHttpError("kt10000", 0, "ReadTimeout")
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock", now=now)
    ex = Executor(db, rest, 1)
    ex.submit(ctx, buy_signal())
    assert "005930" in ex.pending_unknown
    return db, rest, ex


def test_pending_unknown_blocks_rebuy_after_90s():
    """유실 주문 후 90초 뒤 재매수가 나가지 않는다."""
    now = _dt.datetime(2026, 9, 18, 10, 30)
    db, rest, ex = _unknown_executor(now)

    later = now + _dt.timedelta(seconds=90)
    ex.resolve_pending_unknown(holdings={}, cash=10_000_000, now=later)   # 증거 없음
    assert "005930" in ex.pending_unknown

    ctx2 = make_ctx(db, order_enabled=True, trading_mode="mock", now=later)
    res = ex.submit(ctx2, buy_signal())
    assert res.skipped and "접수 여부 불명" in res.blocked_reason
    assert rest.order_call_count == 1            # 재전송 없음


def test_pending_unknown_cleared_only_with_evidence():
    now = _dt.datetime(2026, 9, 18, 10, 30)
    db, _rest, ex = _unknown_executor(now)
    # 보유수량이 변했다 = 체결 증거
    out = ex.resolve_pending_unknown(
        holdings={"005930": {"rmnd_qty": 10}}, cash=10_000_000,
        now=now + _dt.timedelta(seconds=30))
    assert ex.pending_unknown == {}
    assert out["cleared"] and "보유수량" in out["cleared"][0]


def test_pending_unknown_cleared_by_open_order_evidence():
    now = _dt.datetime(2026, 9, 18, 10, 30)
    db, _rest, ex = _unknown_executor(now)
    db.open_order_codes.add("005930")            # ka10075 동기화로 접수 확인
    ex.resolve_pending_unknown(holdings={}, cash=10_000_000, now=now)
    assert ex.pending_unknown == {}


def test_pending_unknown_cleared_by_execution_evidence():
    now = _dt.datetime(2026, 9, 18, 10, 30)
    db, _rest, ex = _unknown_executor(now)
    db.executions.append({"stk_cd": "005930", "executed_at": now})
    ex.resolve_pending_unknown(holdings={}, cash=10_000_000, now=now)
    assert ex.pending_unknown == {}


def test_pending_unknown_timeout_stops_auto_trading():
    """5분 + 연속 3회 동기화로도 확정 못 하면 on_critical 로 자동 정지."""
    now = _dt.datetime(2026, 9, 18, 10, 30)
    db, rest = FakeDb(), FakeRest()
    rest.raise_on["kt10000"] = KiwoomHttpError("kt10000", 0, "ReadTimeout")
    alarms: list[str] = []
    ex = Executor(db, rest, 1, on_critical=alarms.append)
    ex.submit(make_ctx(db, order_enabled=True, trading_mode="mock", now=now), buy_signal())
    alarms.clear()

    for i in range(PENDING_UNKNOWN_MIN_SYNCS):
        ts = now + _dt.timedelta(seconds=310 + i)
        out = ex.resolve_pending_unknown(holdings={}, cash=10_000_000, now=ts)
    assert out["critical"] == ["005930"]
    assert alarms and "확정하지 못했" in alarms[0]
    assert "005930" in ex.pending_unknown         # 해제하지 않는다


def test_pending_unknown_kept_when_lookup_failed():
    """보유/잔고 조회 실패(None)는 '증거 없음'으로 처리해 해제하지 않는다."""
    now = _dt.datetime(2026, 9, 18, 10, 30)
    _db, _rest, ex = _unknown_executor(now)
    ex.resolve_pending_unknown(holdings=None, cash=None, now=now)
    assert "005930" in ex.pending_unknown


def test_engine_review_pending_unknown_uses_evidence():
    db, rest = FakeDb(), FakeRest()
    eng = engine_with(db, rest, [risk_guard_meta()])
    eng.start_auto_trading(by="테스트")
    rest.raise_on["kt10000"] = KiwoomHttpError("kt10000", 0, "ReadTimeout")
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock")
    eng.executor.submit(ctx, buy_signal())
    assert "005930" in eng.executor.pending_unknown

    eng._review_pending_unknown()          # noqa: SLF001 - 증거 없음 → 유지
    assert "005930" in eng.executor.pending_unknown

    db.holding_rows = [{"stk_cd": "005930", "rmnd_qty": 10}]   # 체결 증거 등장
    eng._review_pending_unknown()          # noqa: SLF001
    assert eng.executor.pending_unknown == {}


def test_cooldown_floor_is_default_300():
    db, rest = FakeDb(), FakeRest()
    eng = engine_with(db, rest, [risk_guard_meta()])
    db.settings["poll_interval_sec"] = "5"        # 5초 주기여도
    assert eng.build_context(force_market=True).cooldown_sec >= 300


# ====================================================================== #
# R-04 : 취소도 주문과 같은 수준의 검증
# ====================================================================== #
def test_cancel_blocked_when_gate_closed_in_db_after_build():
    """TOCTOU: ctx 게이트는 열려 있어도 DB 가 닫혔으면 취소를 보내지 않는다."""
    db, rest = FakeDb(), FakeRest()
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock")
    db.settings["order_enabled"] = "0"
    res = Executor(db, rest, 1).submit_cancel(ctx, "0001", "005930", 5)
    assert res.sent is False and rest.order_call_count == 0
    assert "방금 닫힘" in res.blocked_reason


def test_cancel_blocked_on_env_mode_mismatch():
    db, rest = FakeDb(), FakeRest(env="real")
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock")
    alarms: list[str] = []
    res = Executor(db, rest, 1, on_critical=alarms.append).submit_cancel(
        ctx, "0001", "005930", 5)
    assert res.sent is False and rest.order_call_count == 0
    assert "환경 불일치" in res.blocked_reason and alarms


def test_cancel_loop_stops_when_gate_closes_midway():
    db, rest = FakeDb(), FakeRest()
    eng = engine_with(db, rest, [risk_guard_meta()])
    eng.rest = rest
    targets = [{"ord_no": "1", "stk_cd": "005930", "oso_qty": 1, "exchange": "KRX",
                "is_ours": True, "algo_code": "momentum_screen"},
               {"ord_no": "2", "stk_cd": "000660", "oso_qty": 1, "exchange": "KRX",
                "is_ours": True, "algo_code": "momentum_screen"}]

    real_submit = eng.executor.submit_cancel

    def close_gate_after_first(*a, **kw):
        res = real_submit(*a, **kw)
        db.settings["order_enabled"] = "0"      # 1건 보낸 뒤 게이트가 닫힘
        return res

    eng.executor.submit_cancel = close_gate_after_first
    out = eng.cancel_all_open_orders(by="테스트", targets=targets)
    assert out["ok"] == 1 and out["skipped"] == 1
    assert rest.order_call_count == 1
    assert any("게이트가 닫혀" in e for e in out["errors"])


def test_annotate_open_orders_marks_ours():
    db = FakeDb()
    db.order_algos = {"1": "momentum_screen"}       # 2번은 수동(HTS) 주문
    rows = annotate_open_orders(db, 1, [{"ord_no": "1", "stk_cd": "005930", "oso_qty": 3},
                                        {"ord_no": "2", "stk_cd": "000660", "oso_qty": 5}])
    assert rows[0]["is_ours"] is True and rows[1]["is_ours"] is False


def test_cancel_dialog_default_selection_is_ours_only():
    from stock_svr.ui.cancel_dialog import default_selection

    targets = [{"ord_no": "1", "is_ours": True}, {"ord_no": "2", "is_ours": False}]
    assert default_selection(targets) == ["1"]


def test_cancel_only_selected_orders():
    db, rest = FakeDb(), FakeRest()
    eng = engine_with(db, rest, [risk_guard_meta()])
    eng.rest = rest
    targets = [{"ord_no": "1", "stk_cd": "005930", "oso_qty": 1, "exchange": "KRX",
                "is_ours": True},
               {"ord_no": "2", "stk_cd": "000660", "oso_qty": 1, "exchange": "KRX",
                "is_ours": False}]
    out = eng.cancel_all_open_orders(by="테스트", only_ord_nos=["1"], targets=targets)
    assert out["total"] == 1 and out["ok"] == 1
    assert rest.order_calls[0][1]["orig_ord_no"] == "1"


# ====================================================================== #
# R-05 : risk_halt 는 매수만 막는다 (손절 매도는 통과)
# ====================================================================== #
def test_risk_halt_blocks_buy_but_not_stop_loss_sell():
    db, rest = FakeDb(), FakeRest()
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock",
                   holdings={"005930": holding()})
    ctx.halt("총자산 확인 불가")
    ex = Executor(db, rest, 1)

    buy = ex.submit(ctx, buy_signal())
    assert buy.sent is False and "리스크 판단 불가" in buy.blocked_reason

    sell = ex.submit(ctx, sell_signal())
    assert sell.sent is True, sell.blocked_reason
    assert rest.order_calls[-1][0] == "kt10001"


def test_halt_does_not_block_liquidate_sell():
    db, rest = FakeDb(), FakeRest()
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock",
                   holdings={"005930": holding()})
    ctx.halt("position_state 갱신 실패")
    res = Executor(db, rest, 1).submit(ctx, sell_signal(kind=KIND_LIQUIDATE))
    assert res.sent is True


def test_sell_still_blocked_by_closed_gate_and_env_mismatch():
    """halt 를 무시한다고 게이트·env 검사까지 통과하는 것은 아니다."""
    db, rest = FakeDb(), FakeRest(env="real")
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock",
                   holdings={"005930": holding()})
    ctx.halt("테스트")
    assert Executor(db, rest, 1).submit(ctx, sell_signal()).sent is False

    db2, rest2 = FakeDb(), FakeRest()
    ctx2 = make_ctx(db2, order_enabled=False, trading_mode="real",
                    holdings={"005930": holding()})
    ctx2.halt("테스트")
    assert Executor(db2, rest2, 1).submit(ctx2, sell_signal()).sent is False


# ====================================================================== #
# R-06 : 상장폐지·거래불가 종목 제외
# ====================================================================== #
DELISTED = holding("040670", "(폐)와이즈파워", qty=100, cur=0)


def test_delisted_holding_makes_no_stop_loss_signal():
    ctx = make_ctx(FakeDb(), holdings={"040670": dict(DELISTED, prft_rt=-100)})
    assert guard().evaluate(ctx) == []


def test_zero_price_holding_excluded_even_with_name_ok():
    ctx = make_ctx(FakeDb(), holdings={"005930": holding(cur=0, prft_rt=-90)})
    assert guard().evaluate(ctx) == []


def test_stock_master_state_excludes_stock():
    db = FakeDb()
    db.stock_states["005930"] = "거래정지"
    ctx = make_ctx(db, holdings={"005930": holding(prft_rt=-50)})
    assert guard().evaluate(ctx) == []
    ok, why = guard().check(ctx, buy_signal())
    assert ok is False and "거래불가" in why


def test_delisted_blocks_both_buy_and_sell_in_guard():
    ctx = make_ctx(FakeDb(), holdings={"040670": DELISTED})
    ok, why = guard().check(ctx, sell_signal("040670"))
    assert ok is False and "거래불가" in why


def test_executor_blocks_delisted_sell():
    db, rest = FakeDb(), FakeRest()
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock",
                   holdings={"040670": DELISTED})
    res = Executor(db, rest, 1).submit(ctx, sell_signal("040670"))
    assert res.sent is False and rest.order_call_count == 0
    assert "거래불가" in res.blocked_reason


def test_averaging_down_skips_delisted():
    from stock_svr.algo.averaging_down import AveragingDown

    defs = [{"param_key": k, "label": k, "value_type": t, "default_value": d}
            for k, t, d in [("drop_pct", "decimal", "10"),
                            ("step_buy_amount", "int", "100000"),
                            ("max_steps", "int", "3"), ("cooldown_min", "int", "0"),
                            ("order_type", "enum", "3")]]
    algo = AveragingDown(meta={"code": "averaging_down"}, params=ParamSet(defs, {}))
    ctx = make_ctx(FakeDb(), holdings={"040670": dict(DELISTED, prft_rt=-100)},
                   positions={"040670": {"last_buy_price": 20_000}})
    assert algo.evaluate(ctx) == []


def test_untradable_logged_once_per_day():
    db = FakeDb()
    ctx = make_ctx(db, holdings={"040670": DELISTED})
    ctx.untradable_reason("040670")
    ctx2 = make_ctx(db, holdings={"040670": DELISTED})
    ctx2.untradable_reason("040670")
    infos = [m for lvl, _, m in db.events if lvl == "INFO" and "거래불가" in m]
    assert len(infos) == 1


def test_repeated_sell_failures_seal_stock():
    """같은 종목 SELL 이 연속 3회 실패하면 봉인 + 백오프 (무한 재시도 금지)."""
    db, rest = FakeDb(), FakeRest()
    rest.raise_on["kt10001"] = KiwoomHttpError("kt10001", 500, "거부")
    alarms: list[str] = []
    ex = Executor(db, rest, 1, on_critical=alarms.append)
    now = _dt.datetime(2026, 9, 18, 10, 30)

    for i in range(MAX_SELL_FAILURES):
        ts = now + _dt.timedelta(seconds=600 * (i + 1))
        ctx = make_ctx(db, order_enabled=True, trading_mode="mock", now=ts,
                       holdings={"005930": holding()})
        ex.pending_unknown.clear()          # 응답유실 차단과 분리해 실패 누적만 본다
        ex.submit(ctx, sell_signal())

    assert db.position_states[(1, "005930")]["stopped"] == 1
    assert any("봉인" in a for a in alarms)

    ctx = make_ctx(db, order_enabled=True, trading_mode="mock",
                   now=now + _dt.timedelta(days=1), holdings={"005930": holding()})
    ex.pending_unknown.clear()
    blocked = ex.submit(ctx, sell_signal())
    assert blocked.skipped and "봉인" in blocked.blocked_reason


def test_dashboard_shows_delisted_and_real_rate():
    from stock_svr.ui.dashboard_tab import holding_display_rate, holding_is_delisted

    row = {"stk_cd": "040670", "stk_nm": "(폐)와이즈파워", "cur_prc": 0,
           "pur_amt": 1_000_000, "evltv_prft": -1_000_000, "prft_rt": 0}
    assert holding_is_delisted(row) is True
    assert float(holding_display_rate(row)) == pytest.approx(-100.0)
    normal = {"stk_cd": "005930", "stk_nm": "삼성전자", "cur_prc": 70_000, "prft_rt": 5}
    assert holding_is_delisted(normal) is False
    assert holding_display_rate(normal) == 5


def test_unsynced_price_is_not_delisted():
    from stock_svr.ui.dashboard_tab import holding_is_delisted

    assert holding_is_delisted({"stk_nm": "삼성전자", "cur_prc": None}) is False


# ====================================================================== #
# R-07 : 미체결 중복 검사는 같은 side 만
# ====================================================================== #
def test_open_buy_order_does_not_block_stop_loss_sell():
    db, rest = FakeDb(), FakeRest()
    db.open_order_codes.add(("005930", "BUY"))
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock",
                   holdings={"005930": holding()})
    res = Executor(db, rest, 1).submit(ctx, sell_signal())
    assert res.sent is True, res.blocked_reason


def test_open_sell_order_blocks_another_sell():
    db, rest = FakeDb(), FakeRest()
    db.open_order_codes.add(("005930", "SELL"))
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock",
                   holdings={"005930": holding()})
    res = Executor(db, rest, 1).submit(ctx, sell_signal())
    assert res.skipped and "SELL 미체결" in res.blocked_reason


def test_has_open_order_sql_filters_side():
    db = SqlSpyDb()
    db.has_open_order(1, "005930", "SELL")
    assert "AND side=%s" in db.sql[-1][0]


def test_expire_unknown_sent_orders_sql():
    db = SqlSpyDb()
    db.expire_unknown_sent_orders(1, 10)
    sql = db.sql[-1][0]
    assert "ord_no IS NULL" in sql and "status='SENT'" in sql
    assert "UNKNOWN 확정 불가" in sql


def test_engine_expires_unknown_orders():
    db, rest = FakeDb(), FakeRest()
    eng = engine_with(db, rest, [risk_guard_meta()])
    db.orders.append({"id": 1, "ord_no": None, "status": "SENT", "is_dry_run": 0})
    eng._expire_unknown_orders()      # noqa: SLF001
    assert db.orders[0]["status"] == "FAILED"
    assert db.orders[0]["return_msg"] == "UNKNOWN 확정 불가"


# ====================================================================== #
# R-09 : 비밀값 마스킹
# ====================================================================== #
@pytest.mark.parametrize("text", [
    "ANTHROPIC_KEY=sk-ant-api03-abcdef123456",
    "401 Unauthorized: invalid x-api-key sk-ant-api03-ZZZ_test-999",
    "{\"api_key\": \"sk-ant-oat01-longvalue\"}",
])
def test_mask_text_hides_anthropic_key(text):
    out = mask_text(text)
    assert "***" in out
    assert "api03" not in out and "oat01" not in out
    assert "abcdef123456" not in out and "longvalue" not in out


def test_event_log_masks_key():
    db = SqlSpyDb()
    db.log_event("ERROR", "algo", "Claude 오류: key=sk-ant-api03-SECRETVALUE")
    assert db.events and "SECRETVALUE" not in db.events[0][2]
    assert "sk-ant-***" in db.events[0][2]


def test_status_message_masks_key():
    db = SqlSpyDb()
    db.set_status("kiwoom_rest", "error", "token=sk-ant-api03-SECRET")
    assert all("SECRET" not in str(a) for _, args in db.sql for a in args)


def test_mask_account_number_with_label():
    assert "1234567890" not in mask_text("계좌번호=1234567890")
    # 라벨 없는 숫자(주문번호·금액)는 건드리지 않는다
    assert mask_text("ord_no=0001234567890") == "ord_no=0001234567890"


# ====================================================================== #
# R-10 : 게이트 3키는 set_gate 로만
# ====================================================================== #
@pytest.mark.parametrize("key", GATE_KEYS)
def test_set_setting_rejects_gate_keys(key):
    db = SqlSpyDb()
    with pytest.raises(GateChangeError):
        db.set_setting(key, "1")
    assert not any("system_setting" in s for s, _ in db.sql)


def test_set_gate_requires_token():
    db = SqlSpyDb()
    with pytest.raises(GateChangeError):
        db.set_gate("order_enabled", "1", confirm_token="아무거나")


def test_set_gate_writes_and_logs_error_when_opening():
    db = SqlSpyDb()
    db.values["order_enabled"] = "0"
    assert db.set_gate("order_enabled", "1", confirm_token=GATE_CONFIRM_TOKEN) is True
    assert db.values["order_enabled"] == "1"
    assert db.events and db.events[-1][0] == "ERROR"


def test_set_gate_logs_warn_when_closing():
    db = SqlSpyDb()
    db.values["order_enabled"] = "1"
    db.set_gate("order_enabled", "0", confirm_token=GATE_CONFIRM_TOKEN)
    assert db.events[-1][0] == "WARN"


def test_set_gate_noop_when_same_value():
    db = SqlSpyDb()
    db.values["trading_mode"] = "real"
    assert db.set_gate("trading_mode", "real", confirm_token=GATE_CONFIRM_TOKEN) is False
    assert db.events == []


def test_set_gate_rejects_non_gate_key():
    db = SqlSpyDb()
    with pytest.raises(GateChangeError):
        db.set_gate("poll_interval_sec", "30", confirm_token=GATE_CONFIRM_TOKEN)


def test_normal_setting_still_saves():
    db = SqlSpyDb()
    db.set_setting("poll_interval_sec", "30")
    assert db.values["poll_interval_sec"] == "30"


# ====================================================================== #
# R-11 : 확인창 이후 게이트가 더 열렸으면 시작 거부
# ====================================================================== #
def gate(enabled="0", mode="real", confirm="0") -> OrderGateState:
    return OrderGateState.from_settings(
        {"order_enabled": enabled, "trading_mode": mode, "real_trading_confirm": confirm})


@pytest.mark.parametrize("before,after,expected", [
    (gate("0"), gate("1", "mock"), True),                      # OFF → ON
    (gate("1", "mock"), gate("1", "real", "1"), True),         # mock → real
    (gate("1", "mock"), gate("1", "mock"), False),             # 동일
    (gate("1", "mock"), gate("0"), False),                     # 더 닫힘
    (gate("1", "real", "1"), gate("1", "real", "1"), False),
])
def test_gate_widened(before, after, expected):
    assert gate_widened(before, after) is expected


def test_start_auto_trading_refused_when_gate_widened():
    db, rest = FakeDb(), FakeRest()
    eng = engine_with(db, rest, [risk_guard_meta()])
    shown = gate("0")                       # 확인창에는 '닫힘'으로 보여줬는데
    db.settings["order_enabled"] = "1"      # 그 사이 누군가 열었다
    assert eng.start_auto_trading(by="테스트", shown_gate=shown) is False
    assert eng.auto_trading_active is False
    assert "더 열렸" in eng.start_refused_reason
    assert any(lvl == "ERROR" for lvl, _, _ in db.events)


def test_start_auto_trading_allowed_when_gate_same():
    db, rest = FakeDb(), FakeRest()
    eng = engine_with(db, rest, [risk_guard_meta()])
    shown = eng.current_gate()
    assert eng.start_auto_trading(by="테스트", shown_gate=shown) is True


# ====================================================================== #
# R-12 : 사이클 누적에도 슬리피지 버퍼
# ====================================================================== #
def test_cycle_invest_uses_slippage_buffer_for_market_order():
    db, rest = FakeDb(), FakeRest()
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock")
    sig = buy_signal(qty=10, price=10_000)
    sig.trde_tp = "3"                      # 시장가
    Executor(db, rest, 1).submit(ctx, sig)
    assert ctx.cycle_invested == 110_000   # 100,000 × 1.1
    assert amount_with_buffer(sig) == 110_000


def test_cycle_invest_has_no_buffer_for_limit_order():
    db, rest = FakeDb(), FakeRest()
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock")
    Executor(db, rest, 1).submit(ctx, buy_signal(qty=10, price=10_000))
    assert ctx.cycle_invested == 100_000


# ====================================================================== #
# R-13 : --allow-orders 는 효과가 없다(이중 잠금)
# ====================================================================== #
def test_allow_orders_help_states_no_effect():
    from stock_svr.__main__ import build_parser

    action = next(a for a in build_parser()._actions      # noqa: SLF001
                  if "--allow-orders" in (a.option_strings or []))
    assert "효과 없음" in action.help


def test_double_lock_blocks_orders_even_with_gate_open():
    """게이트가 열려 있어도 자동거래가 중지면 주문은 나가지 않는다."""
    db, rest = FakeDb(), FakeRest()
    eng = engine_with(db, rest, [risk_guard_meta(trade_start_time="", trade_end_time=""),
                                 momentum_meta()])
    assert eng.current_gate().can_send_order is True
    assert eng.auto_trading_active is False
    result = eng.run_cycle(force_market=True, ignore_auto=True, observe_only=False)
    assert result["sent"] == 0 and rest.order_call_count == 0
    assert all(o["is_dry_run"] == 1 for o in db.orders)


# ====================================================================== #
# R-14 : 연속조회가 잘리면 보유종목을 지우지 않는다
# ====================================================================== #
KT00018 = {
    "return_code": 0, "tot_pur_amt": "1000000", "tot_evlt_amt": "1050000",
    "tot_evlt_pl": "+50000", "tot_prft_rt": "5.00", "prsm_dpst_aset_amt": "2050000",
    "acnt_evlt_remn_indv_tot": [
        {"stk_cd": "A005930", "stk_nm": "삼성전자", "rmnd_qty": "10", "trde_able_qty": "10",
         "pur_pric": "60000", "cur_prc": "+63000", "pur_amt": "600000",
         "evlt_amt": "630000", "evltv_prft": "+30000", "prft_rt": "5.00", "poss_rt": "30.00"},
    ],
}


class _HoldingSpyDb(FakeDb):
    def __init__(self):
        super().__init__()
        self.delete_missing_calls: list[bool] = []

    def insert_balance(self, account_id, snapshot_at, data):
        return 1

    def replace_holdings(self, account_id, holdings, delete_missing=True):
        self.delete_missing_calls.append(delete_missing)
        return len(holdings)

    def reset_stale_positions(self, account_id, held, today):
        return 0


def test_truncated_evaluation_skips_holding_replacement():
    from stock_svr.services.sync_account import AccountService

    db = _HoldingSpyDb()
    rest = FakeRest({"kt00001": {"return_code": 0}, "kt00018": KT00018})
    rest.paged_truncated = True
    svc = AccountService(db, rest)
    out = svc.sync(1)
    assert out["truncated"] is True
    assert db.delete_missing_calls == [False]
    assert any("불완전" in m for _, _, m in db.events)


def test_complete_evaluation_replaces_holdings():
    from stock_svr.services.sync_account import AccountService

    db = _HoldingSpyDb()
    rest = FakeRest({"kt00001": {"return_code": 0}, "kt00018": KT00018})
    svc = AccountService(db, rest)
    out = svc.sync(1)
    assert out["truncated"] is False and db.delete_missing_calls == [True]


# ====================================================================== #
# R-15 : 일 주문 횟수는 우리 알고리즘의 신규 주문만
# ====================================================================== #
def test_count_orders_today_excludes_manual_orders():
    db = SqlSpyDb()
    db.count_orders_today(1, only_sent=True)
    sql = db.sql[-1][0]
    assert "algo_code IS NOT NULL" in sql and "order_kind='NEW'" in sql


def test_count_orders_today_all_has_no_filter():
    db = SqlSpyDb()
    db.count_orders_today(1, only_sent=False)
    assert "algo_code IS NOT NULL" not in db.sql[-1][0]


# ====================================================================== #
# R-16 : 기준 자산 0 은 '판정 생략'이 아니라 차단
# ====================================================================== #
def test_zero_base_asset_blocks_with_explicit_reason():
    ctx = make_ctx(FakeDb(), balance={"ord_alow_amt": 1_000_000, "entr": 1_000_000,
                                      "prsm_dpst_aset_amt": 0})
    ok, why = guard().check(ctx, buy_signal())
    assert ok is False and "확인 불가" in why
    assert ctx.risk_halt


# ====================================================================== #
# R-08 : 다중 인스턴스 락(원자적 · 고정 경로)
# ====================================================================== #
def test_lock_path_is_fixed_outside_exe_dir():
    p = si.lock_path()
    assert p.name == si.LOCK_NAME
    assert p.parent.name == si.APP_DIR_NAME
    base = os.environ.get("LOCALAPPDATA")
    if base:
        assert str(p).startswith(base)


def test_second_process_cannot_acquire_same_lock(tmp_path):
    """다른 프로세스가 락을 잡고 있으면 AlreadyRunningError."""
    lock = tmp_path / "stock_svr.lock"
    server_dir = str(SERVER_DIR)
    code = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {server_dir!r})
        from pathlib import Path
        from stock_svr.single_instance import acquire
        acquire(Path({str(lock)!r}))
        print("LOCKED", flush=True)
        time.sleep(20)
    """)
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "LOCKED"
        with pytest.raises(si.AlreadyRunningError):
            si.acquire(lock)
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_dead_pid_lock_is_reclaimed(tmp_path):
    lock = tmp_path / "stock_svr.lock"
    lock.write_text("999999999", encoding="utf-8")      # 존재하지 않는 PID
    got = si.acquire(lock)
    try:
        assert got == lock
        assert lock.read_text(encoding="utf-8").strip() == str(os.getpid())
    finally:
        si.release(lock)
    assert lock.exists() is False


def test_legacy_lock_of_running_old_build_is_detected(tmp_path, monkeypatch):
    """구버전(exe 옆 logs\\stock_svr.pid)이 떠 있으면 새 빌드는 기동을 거부한다."""
    legacy = tmp_path / "logs" / "stock_svr.pid"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(str(os.getppid() or 1), encoding="utf-8")
    monkeypatch.setattr(si, "legacy_lock_path", lambda: legacy)
    monkeypatch.setattr(si, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(si, "lock_path", lambda: tmp_path / "stock_svr.lock")
    with pytest.raises(si.AlreadyRunningError):
        si.acquire()


def test_legacy_lock_of_dead_pid_is_ignored(tmp_path, monkeypatch):
    legacy = tmp_path / "logs" / "stock_svr.pid"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("999999999", encoding="utf-8")
    monkeypatch.setattr(si, "legacy_lock_path", lambda: legacy)
    monkeypatch.setattr(si, "lock_path", lambda: tmp_path / "stock_svr.lock")
    try:
        assert si.acquire() == tmp_path / "stock_svr.lock"
    finally:
        si.release(tmp_path / "stock_svr.lock")


def test_same_process_reacquire_is_idempotent(tmp_path):
    lock = tmp_path / "stock_svr.lock"
    with si.single_instance(lock):
        assert si.acquire(lock) == lock
    assert lock.exists() is False
