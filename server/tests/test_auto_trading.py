"""자동거래(알고리즘 평가·주문) 시작/중지 스위치 테스트.

자동거래는 주문 게이트와 **별개의 선행 조건**이다.
  · 중지 상태 → 평가/신호/주문 모두 0건
  · 시작 + 게이트 닫힘 → SIGNAL_ONLY 만
  · 시작 + 게이트 열림 → fake 클라이언트 주문 호출
  · 사이클 도중 중지 → 이후 주문 미전송
"""
from __future__ import annotations

import pytest

from conftest import FakeDb, FakeMarket, make_ctx
from stock_svr.algo.base import Signal
from stock_svr.config import AppConfig
from stock_svr.engine.executor import Executor
from stock_svr.engine.runner import (
    AUTO_COMPONENT,
    AUTO_TEXT_OBSERVE,
    AUTO_TEXT_ORDER_ON,
    AUTO_TEXT_STOPPED,
    Engine,
)
from stock_svr.kiwoom.fake import FakeRest
from stock_svr.util import now_kst
from tests_support import RISK_DEFS

MOM_DEFS = [
    {"param_key": k, "label": k, "value_type": t, "default_value": d, "enum_options": eo}
    for k, t, d, eo in [
        ("market", "enum", "000", "000:전체"),
        ("min_flu_rt", "decimal", "3", None),
        ("max_flu_rt", "decimal", "15", None),
        ("min_volume_surge_rt", "decimal", "100", None),
        ("min_trde_qty", "int", "0", None),
        ("min_price", "int", "0", None),
        ("exclude_etf", "bool", "0", None),
        ("top_n", "int", "5", None),
        ("buy_amount", "int", "100000", None),
        ("max_new_per_day", "int", "3", None),
        ("order_type", "enum", "3", "3:시장가,0:지정가(보통)"),
    ]
]


def risk_guard_meta(**params) -> dict:
    """항상 켜져 있는 risk_guard 알고리즘 행(테스트용)."""
    values = {d["param_key"]: d["default_value"] for d in RISK_DEFS}
    values.update({k: str(v) for k, v in params.items()})
    return {"id": 1, "code": "risk_guard", "name": "리스크 가드", "role": "risk",
            "is_enabled": 1, "is_locked": 1, "priority": 1,
            "param_defs": RISK_DEFS, "params": values}


def _signal(stk="005930", qty=10, price=1000):
    return Signal(algo_code="momentum_screen", stk_cd=stk, stk_nm="삼성전자", side="BUY",
                  qty=qty, price=price, trde_tp="0", amount=qty * price, reason="테스트")


def _engine(db: FakeDb, rest: FakeRest, auto_check=None) -> Engine:
    """DB/네트워크 없이 run_cycle 만 검증하는 엔진 조립."""
    eng = Engine(AppConfig(), db)
    eng.account_id = 1
    eng.run_id = 7
    eng.market = FakeMarket(
        ranks=[{"rank_no": 1, "stk_cd": "005930", "stk_nm": "삼성전자", "cur_prc": 10000,
                "flu_rt": 5, "now_trde_qty": 100000, "sdnin_rt": None}],
        surges=[{"rank_no": 1, "stk_cd": "005930", "stk_nm": "삼성전자", "cur_prc": 10000,
                 "flu_rt": 5, "now_trde_qty": 100000, "sdnin_rt": 300}])
    eng.executor = Executor(db, rest, 1, 7,
                            auto_trading_check=auto_check or (lambda: eng.auto_trading_active))
    # R-01: risk_guard 가 없으면 사이클이 중단되므로(fail-closed) 정상 시나리오에는 함께 싣는다
    db.algorithms = [
        # 매매시간 파라미터를 비워 실행 시각과 무관하게 동작하도록 한다(시한폭탄 테스트 방지)
        risk_guard_meta(trade_start_time="", trade_end_time=""),
        {"id": 2, "code": "momentum_screen", "name": "모멘텀", "role": "entry",
         "is_enabled": 1, "is_locked": 0, "priority": 10,
         "param_defs": MOM_DEFS, "params": {d["param_key"]: d["default_value"] for d in MOM_DEFS}},
    ]
    db.balance = {"entr": 10_000_000, "ord_alow_amt": 10_000_000,
                  "prsm_dpst_aset_amt": 10_000_000, "snapshot_at": now_kst()}
    return eng


# ====================================================================== #
# (e) 기동 직후 기본값 = 중지
# ====================================================================== #
def test_auto_trading_default_is_stopped():
    eng = Engine(AppConfig(), FakeDb())
    assert eng.auto_trading_active is False
    assert eng.status.snapshot()["auto_trading"] is False
    assert eng.status.snapshot()["auto_status"][1] == AUTO_TEXT_STOPPED


def test_start_and_stop_toggle():
    db = FakeDb()
    eng = Engine(AppConfig(), db)
    assert eng.start_auto_trading(by="테스트") is True
    assert eng.auto_trading_active is True
    assert eng.start_auto_trading() is False          # 중복 시작은 무시
    assert db.statuses[AUTO_COMPONENT][0] == "ok"
    assert db.statuses[AUTO_COMPONENT][1] == AUTO_TEXT_OBSERVE

    eng.stop_auto_trading(by="테스트")
    assert eng.auto_trading_active is False
    assert db.statuses[AUTO_COMPONENT] == ("unknown", AUTO_TEXT_STOPPED)


def test_status_shows_order_on_when_gate_open():
    db = FakeDb()
    db.settings = {"order_enabled": "1", "trading_mode": "mock", "real_trading_confirm": "0"}
    eng = Engine(AppConfig(), db)
    eng.start_auto_trading()
    status, text = db.statuses[AUTO_COMPONENT]
    assert status == "warn" and text.startswith(AUTO_TEXT_ORDER_ON)


# ====================================================================== #
# (a) 중지 상태 → 평가/신호/주문 0건
# ====================================================================== #
def test_stopped_engine_does_not_evaluate():
    db, rest = FakeDb(), FakeRest()
    eng = _engine(db, rest)
    result = eng.run_cycle(force_market=True)
    assert result["skipped"] == "자동거래 중지"
    assert result["signals"] == [] and result["algos"] == []
    assert db.signals == [] and db.orders == []
    assert rest.calls == []


def test_eval_once_cli_path_ignores_auto_switch():
    """--eval-once 진단 경로는 자동거래 중지 상태에서도 평가한다(관찰 전용)."""
    db, rest = FakeDb(), FakeRest()
    eng = _engine(db, rest)
    result = eng.run_cycle(ignore_auto=True, observe_only=True, force_market=True)
    assert result["ctx"] is not None
    assert result["ctx"].gate.can_send_order is False     # S-09 관찰 전용 강제
    assert rest.order_call_count == 0


# ====================================================================== #
# (b)/(c) 시작 후 게이트에 따른 동작
# ====================================================================== #
def test_started_with_closed_gate_records_signal_only():
    db, rest = FakeDb(), FakeRest()
    eng = _engine(db, rest)
    eng.start_auto_trading()
    result = eng.run_cycle(force_market=True)
    assert result["sent"] == 0
    assert rest.order_call_count == 0
    assert db.orders and all(o["is_dry_run"] == 1 for o in db.orders)
    assert all(o["status"] == "SIGNAL_ONLY" for o in db.orders)


def test_started_with_open_gate_calls_order_api():
    db, rest = FakeDb(), FakeRest()
    db.settings = {"order_enabled": "1", "trading_mode": "mock", "real_trading_confirm": "0"}
    eng = _engine(db, rest)
    eng.start_auto_trading()
    result = eng.run_cycle(force_market=True)
    assert result["sent"] == 1
    assert rest.order_calls and rest.order_calls[0][0] == "kt10000"
    assert db.sent_orders and db.sent_orders[0]["status"] == "SENT"


# ====================================================================== #
# (d) 사이클 도중 중지 → 이후 주문 미전송
# ====================================================================== #
def test_stop_mid_cycle_halts_further_orders():
    db, rest = FakeDb(), FakeRest()
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock")
    active = {"on": True}
    ex = Executor(db, rest, 1, auto_trading_check=lambda: active["on"])

    first = ex.submit(ctx, _signal("005930"))
    assert first.sent and rest.order_call_count == 1

    active["on"] = False          # 사이클 도중 중지 버튼
    second = ex.submit(ctx, _signal("000660"))
    assert second.sent is False
    assert second.status == "SIGNAL_ONLY"
    assert "자동거래 중지" in second.blocked_reason
    assert rest.order_call_count == 1     # 추가 전송 없음
    assert db.orders[-1]["is_dry_run"] == 1


def test_auto_check_exception_is_fail_closed():
    db, rest = FakeDb(), FakeRest()
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock")

    def boom():
        raise RuntimeError("상태 확인 불가")

    res = Executor(db, rest, 1, auto_trading_check=boom).submit(ctx, _signal())
    assert res.sent is False and rest.order_call_count == 0


# ====================================================================== #
# (f) 엔진 정지 시 자동거래 동반 중지
# ====================================================================== #
def test_engine_shutdown_stops_auto_trading():
    db = FakeDb()
    eng = Engine(AppConfig(), db)
    eng.start_auto_trading()
    assert eng.auto_trading_active is True
    eng._shutdown()   # noqa: SLF001 - 종료 경로 직접 호출
    assert eng.auto_trading_active is False
    assert db.statuses[AUTO_COMPONENT] == ("unknown", AUTO_TEXT_STOPPED)
    assert eng.status.snapshot()["auto_trading"] is False


def test_stop_reports_open_order_count():
    db = FakeDb()
    db.open_order_codes = {"005930", "000660"}
    eng = Engine(AppConfig(), db)
    eng.account_id = 1
    eng.start_auto_trading()
    assert eng.stop_auto_trading(by="테스트") == 2


# ====================================================================== #
# 긴급 미체결 취소 (S-15) — fake 클라이언트만 사용
# ====================================================================== #
def test_cancel_open_orders_requires_open_gate():
    db, rest = FakeDb(), FakeRest()
    ctx = make_ctx(db, order_enabled=False, trading_mode="real")
    res = Executor(db, rest, 1).submit_cancel(ctx, "0001", "005930", 5)
    assert res.sent is False and rest.order_call_count == 0
    assert db.orders[0]["order_kind"] == "CANCEL" and db.orders[0]["is_dry_run"] == 1


def test_cancel_open_orders_sends_kt10003():
    db, rest = FakeDb(), FakeRest()
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock")
    res = Executor(db, rest, 1).submit_cancel(ctx, "0001", "005930", 5)
    assert res.sent is True
    assert rest.order_calls[0][0] == "kt10003"
    assert rest.order_calls[0][1]["orig_ord_no"] == "0001"
    assert rest.order_calls[0][1]["cncl_qty"] == "5"


def test_cancel_works_even_when_auto_trading_stopped():
    """긴급 취소는 자동거래 중지 상태에서도 동작해야 한다."""
    db, rest = FakeDb(), FakeRest()
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock")
    ex = Executor(db, rest, 1, auto_trading_check=lambda: False)
    assert ex.submit_cancel(ctx, "0002", "005930", 3).sent is True


@pytest.mark.parametrize("api", ["kt10000", "kt10001", "kt10003"])
def test_all_order_paths_go_through_unlock(api):
    rest = FakeRest()
    from stock_svr.kiwoom.errors import OrderBlockedError
    with pytest.raises(OrderBlockedError):
        rest.call(api, {})


def test_startup_corrects_stale_running_status_after_forced_kill():
    """이전 프로세스가 강제 종료돼(재배포 등) DB 에 '실행중'이 남아 있어도,
    부팅 시 실제 상태(중지)와 표시를 일치시켜야 한다."""
    db = FakeDb()
    db.statuses[AUTO_COMPONENT] = ("warn", AUTO_TEXT_ORDER_ON)  # 강제종료로 남은 오래된 값
    eng = Engine(AppConfig(), db)
    assert eng.auto_trading_active is False              # 실제 상태는 이미 중지(Event 미설정)
    assert db.statuses[AUTO_COMPONENT][1] == AUTO_TEXT_ORDER_ON  # 아직 표시는 안 고쳐짐

    eng.reset_auto_status_stopped()                       # _startup() 이 부팅 시 호출하는 것

    assert db.statuses[AUTO_COMPONENT] == ("unknown", AUTO_TEXT_STOPPED)
    assert eng.status.snapshot()["auto_status"] == ("unknown", AUTO_TEXT_STOPPED)
