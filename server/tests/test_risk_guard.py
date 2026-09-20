"""risk_guard 한도 검증 테스트."""
from __future__ import annotations

import datetime as _dt

import pytest

from conftest import FakeDb, make_ctx
from stock_svr.algo.base import Signal
from stock_svr.algo.params import ParamSet
from stock_svr.algo.risk_guard import RiskGuard

DEFS = [
    {"param_key": "max_total_invest", "label": "총 투입 한도", "value_type": "int",
     "default_value": "1000000"},
    {"param_key": "max_invest_per_stock", "label": "종목당 최대 투입금", "value_type": "int",
     "default_value": "300000"},
    {"param_key": "stop_loss_pct", "label": "손절 라인", "value_type": "decimal",
     "default_value": "-15"},
    {"param_key": "daily_loss_limit_pct", "label": "일 손실 한도", "value_type": "decimal",
     "default_value": "-3"},
    {"param_key": "max_orders_per_day", "label": "일 최대 주문 횟수", "value_type": "int",
     "default_value": "30"},
    {"param_key": "trade_start_time", "label": "매매 시작 시각", "value_type": "time",
     "default_value": "09:05"},
    {"param_key": "trade_end_time", "label": "신규진입 종료 시각", "value_type": "time",
     "default_value": "15:15"},
    {"param_key": "exchange", "label": "거래소", "value_type": "enum",
     "default_value": "KRX", "enum_options": "KRX:KRX,NXT:NXT,SOR:SOR"},
]


def guard(**overrides) -> RiskGuard:
    return RiskGuard(meta={"code": "risk_guard", "name": "리스크 가드"},
                     params=ParamSet(DEFS, overrides))


def buy(qty=10, price=10000, stk="005930"):
    return Signal(algo_code="momentum_screen", stk_cd=stk, side="BUY", qty=qty,
                  price=price, amount=qty * price, reason="테스트")


# ====================================================================== #
def test_pass_within_limits():
    ok, reason = guard().check(make_ctx(FakeDb()), buy())
    assert ok, reason


def test_block_outside_market_hours():
    ctx = make_ctx(FakeDb(), market_open=False)
    ok, reason = guard().check(ctx, buy())
    assert not ok and "장 시간" in reason


def test_block_before_trade_start_time():
    ctx = make_ctx(FakeDb(), now=_dt.datetime(2026, 9, 18, 9, 1))
    ok, reason = guard().check(ctx, buy())
    assert not ok and "매매시간" in reason


def test_block_after_trade_end_time():
    ctx = make_ctx(FakeDb(), now=_dt.datetime(2026, 9, 18, 15, 20))
    ok, reason = guard().check(ctx, buy())
    assert not ok and "매매시간" in reason


def test_block_max_orders_per_day():
    db = FakeDb()
    db.orders_today = 30
    ok, reason = guard().check(make_ctx(db), buy())
    assert not ok and "주문 횟수" in reason


def test_block_daily_loss_limit():
    db = FakeDb()
    db.realized_pl = -400_000     # 자산 10,000,000 대비 -4% < -3%
    ok, reason = guard().check(make_ctx(db), buy())
    assert not ok and "일 손실 한도" in reason


def test_daily_loss_within_limit_passes():
    db = FakeDb()
    db.realized_pl = -100_000     # -1%
    ok, _ = guard().check(make_ctx(db), buy())
    assert ok


def test_block_total_invest_limit():
    db = FakeDb()
    positions = {"000660": {"total_invested": 950_000, "avg_down_count": 0}}
    ok, reason = guard().check(make_ctx(db, positions=positions), buy(qty=10, price=10000))
    assert not ok and "총 절대 한도" in reason


def test_block_per_stock_limit():
    positions = {"005930": {"total_invested": 295_000, "avg_down_count": 1}}
    ok, reason = guard().check(make_ctx(FakeDb(), positions=positions), buy(qty=10, price=10000))
    assert not ok and "종목당 절대 한도" in reason


def test_block_insufficient_cash():
    ctx = make_ctx(FakeDb(), balance={"ord_alow_amt": 50_000, "prsm_dpst_aset_amt": 1_000_000})
    ok, reason = guard().check(ctx, buy(qty=10, price=10000))
    assert not ok and "주문가능금액" in reason


def test_block_stopped_position():
    positions = {"005930": {"stopped": 1, "total_invested": 0}}
    ok, reason = guard().check(make_ctx(FakeDb(), positions=positions), buy())
    assert not ok and "재진입 금지" in reason


# -- 매도 --------------------------------------------------------------- #
def test_sell_requires_holding():
    sig = Signal(algo_code="risk_guard", stk_cd="005930", side="SELL", qty=10)
    ok, reason = guard().check(make_ctx(FakeDb()), sig)
    assert not ok and "보유수량" in reason


def test_sell_qty_capped_to_tradable():
    holdings = {"005930": {"stk_cd": "005930", "rmnd_qty": 10, "trde_able_qty": 4}}
    sig = Signal(algo_code="risk_guard", stk_cd="005930", side="SELL", qty=10)
    ok, _ = guard().check(make_ctx(FakeDb(), holdings=holdings), sig)
    assert ok and sig.qty == 4


# -- 손절 신호 ---------------------------------------------------------- #
def test_stop_loss_signal_generated():
    holdings = {"005930": {"stk_cd": "005930", "stk_nm": "삼성전자", "rmnd_qty": 10,
                           "trde_able_qty": 10, "prft_rt": -20}}
    sigs = guard().evaluate(make_ctx(FakeDb(), holdings=holdings))
    assert len(sigs) == 1
    assert sigs[0].side == "SELL" and sigs[0].qty == 10 and sigs[0].kind == "stop_loss"


def test_no_stop_loss_above_line():
    holdings = {"005930": {"stk_cd": "005930", "rmnd_qty": 10, "prft_rt": -5}}
    assert guard().evaluate(make_ctx(FakeDb(), holdings=holdings)) == []


def test_no_stop_loss_when_market_closed():
    holdings = {"005930": {"stk_cd": "005930", "rmnd_qty": 10, "prft_rt": -50}}
    assert guard().evaluate(make_ctx(FakeDb(), holdings=holdings, market_open=False)) == []


def test_exchange_param_applied():
    sig = buy()
    sig.exchange = ""
    guard(exchange="NXT").check(make_ctx(FakeDb()), sig)
    assert sig.exchange == "NXT"


@pytest.mark.parametrize("pct,expected", [(-14.9, 0), (-15, 1), (-15.1, 1)])
def test_stop_loss_boundary(pct, expected):
    holdings = {"005930": {"stk_cd": "005930", "rmnd_qty": 5, "trde_able_qty": 5, "prft_rt": pct}}
    assert len(guard().evaluate(make_ctx(FakeDb(), holdings=holdings))) == expected
