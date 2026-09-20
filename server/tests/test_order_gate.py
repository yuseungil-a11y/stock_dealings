"""주문 게이트 전수 테스트 (DEV_SPEC 2-3).

    can_send_order = order_enabled AND (trading_mode == 'mock' OR real_trading_confirm)

게이트가 닫히면 fake 클라이언트가 **절대** 호출되지 않고 `orders` 에
`is_dry_run=1, status='SIGNAL_ONLY'` 로만 기록되어야 한다.
"""
from __future__ import annotations

import datetime as _dt
import itertools

import pytest

from conftest import FakeDb, make_ctx
from stock_svr.algo.base import Signal
from stock_svr.engine.context import OrderGateState
from stock_svr.engine.executor import Executor
from stock_svr.kiwoom.errors import OrderBlockedError
from stock_svr.kiwoom.fake import FakeRest
from stock_svr.kiwoom.rest import ORDER_API_IDS


def expected_can_send(order_enabled: bool, mode: str, confirm: bool) -> bool:
    return order_enabled and (mode == "mock" or confirm)


ALL_COMBOS = list(itertools.product([False, True], ["mock", "real"], [False, True]))


# ====================================================================== #
@pytest.mark.parametrize("order_enabled,mode,confirm", ALL_COMBOS)
def test_gate_state_truth_table(order_enabled, mode, confirm):
    gate = OrderGateState.from_settings({
        "order_enabled": "1" if order_enabled else "0",
        "trading_mode": mode,
        "real_trading_confirm": "1" if confirm else "0",
    })
    assert gate.can_send_order is expected_can_send(order_enabled, mode, confirm)
    if not gate.can_send_order:
        assert gate.block_reason() != ""
    else:
        assert gate.block_reason() == ""


def test_gate_defaults_are_closed():
    """설정이 비어 있으면(기본값) 게이트는 닫혀 있어야 한다."""
    assert OrderGateState().can_send_order is False
    assert OrderGateState.from_settings({}).can_send_order is False


# ====================================================================== #
def _signal(side="BUY", qty=10, price=1000):
    return Signal(algo_code="momentum_screen", stk_cd="005930", stk_nm="삼성전자",
                  side=side, qty=qty, price=price, trde_tp="0", amount=qty * price,
                  reason="테스트 신호")


@pytest.mark.parametrize("order_enabled,mode,confirm", ALL_COMBOS)
def test_executor_calls_api_only_when_gate_open(order_enabled, mode, confirm):
    db = FakeDb()
    rest = FakeRest()
    ctx = make_ctx(db, order_enabled=order_enabled, trading_mode=mode, real_confirm=confirm)
    ex = Executor(db, rest, account_id=1, run_id=7)

    result = ex.submit(ctx, _signal())
    can_send = expected_can_send(order_enabled, mode, confirm)

    assert result.sent is can_send
    assert rest.order_call_count == (1 if can_send else 0)
    assert len(db.orders) == 1
    row = db.orders[0]
    if can_send:
        assert row["is_dry_run"] == 0
        assert row["status"] == "SENT"
        assert row["ord_no"] and row["ord_no"].startswith("FAKE")
        assert rest.order_calls[0][0] == "kt10000"
    else:
        assert row["is_dry_run"] == 1
        assert row["status"] == "SIGNAL_ONLY"
        assert row["ord_no"] is None
        assert rest.calls == []           # 어떤 API 도 호출되지 않음
        assert result.blocked_reason


@pytest.mark.parametrize("side,api_id", [("BUY", "kt10000"), ("SELL", "kt10001")])
def test_executor_uses_correct_order_api(side, api_id):
    db, rest = FakeDb(), FakeRest()
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock")
    ex = Executor(db, rest, account_id=1)
    ex.submit(ctx, _signal(side=side))
    assert rest.order_calls[0][0] == api_id


def test_signal_is_always_logged_even_when_blocked():
    db, rest = FakeDb(), FakeRest()
    ctx = make_ctx(db, order_enabled=False, trading_mode="real")
    Executor(db, rest, 1, run_id=3).submit(ctx, _signal())
    assert db.signals and db.signals[0]["algo_code"] == "momentum_screen"
    assert db.signals[0]["order_id"] == db.orders[0]["id"]


def test_zero_qty_is_skipped():
    db, rest = FakeDb(), FakeRest()
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock")
    res = Executor(db, rest, 1).submit(ctx, _signal(qty=0))
    assert res.skipped and not db.orders and rest.order_call_count == 0


def test_open_order_blocks_duplicate():
    db, rest = FakeDb(), FakeRest()
    db.open_order_codes.add("005930")
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock")
    res = Executor(db, rest, 1).submit(ctx, _signal())
    assert res.skipped and rest.order_call_count == 0
    assert db.signals[0]["signal_type"] == "BLOCK"


def test_cooldown_blocks_repeat_signal():
    db, rest = FakeDb(), FakeRest()
    now = _dt.datetime(2026, 9, 18, 10, 30, 0)
    db.last_order_times[("005930", "BUY")] = now - _dt.timedelta(seconds=30)
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock", now=now, cooldown_sec=300)
    res = Executor(db, rest, 1).submit(ctx, _signal())
    assert res.skipped and "쿨다운" in res.blocked_reason
    assert rest.order_call_count == 0


def test_order_api_blocked_outside_unlock():
    """Executor 를 거치지 않는 직접 호출은 차단된다(우회 경로 방지)."""
    rest = FakeRest()
    for api_id in ("kt10000", "kt10001", "kt10002", "kt10003"):
        with pytest.raises(OrderBlockedError):
            rest.call(api_id, {})
    assert rest.order_call_count == 0


def test_order_api_ids_cover_spec():
    for api_id in ("kt10000", "kt10001", "kt10002", "kt10003"):
        assert api_id in ORDER_API_IDS


def test_api_failure_marks_order_failed():
    from stock_svr.kiwoom.errors import KiwoomApiError

    db, rest = FakeDb(), FakeRest()
    rest.raise_on["kt10000"] = KiwoomApiError("kt10000", 1234, "실패")
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock")
    res = Executor(db, rest, 1).submit(ctx, _signal())
    assert res.error and res.status == "FAILED"
    assert db.orders[0]["status"] == "FAILED"


def test_position_state_updated_only_on_real_send():
    db, rest = FakeDb(), FakeRest()
    ctx = make_ctx(db, order_enabled=False, trading_mode="real")
    Executor(db, rest, 1).submit(ctx, _signal())
    assert db.position_states == {}

    db2, rest2 = FakeDb(), FakeRest()
    ctx2 = make_ctx(db2, order_enabled=True, trading_mode="mock")
    Executor(db2, rest2, 1).submit(ctx2, _signal())
    assert db2.position_states[(1, "005930")]["total_invested"] == 10000
