"""각 알고리즘 단위테스트 (fake 시세 데이터)."""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

import pytest

from conftest import FakeDb, FakeMarket, make_ctx
from stock_svr.algo import registry
from stock_svr.algo.averaging_down import AveragingDown
from stock_svr.algo.base import Signal
from stock_svr.algo.ma_cross_filter import MaCrossFilter, moving_average
from stock_svr.algo.momentum_screen import MomentumScreen
from stock_svr.algo.params import ParamSet
from stock_svr.algo.volatility_breakout import VolatilityBreakout, target_price


def defs(*items):
    return [{"param_key": k, "label": k, "value_type": t, "default_value": d,
             "enum_options": eo} for k, t, d, eo in items]


# ====================================================================== #
# 레지스트리
# ====================================================================== #
def test_registry_has_all_seed_codes():
    codes = set(registry.known_codes())
    assert codes >= {"risk_guard", "momentum_screen", "volatility_breakout",
                     "averaging_down", "ma_cross_filter"}


def test_build_all_respects_selection_and_lock():
    metas = [
        {"id": 1, "code": "risk_guard", "name": "가드", "is_enabled": 0, "is_locked": 1,
         "priority": 1, "param_defs": [], "params": {}},
        {"id": 2, "code": "momentum_screen", "name": "모멘텀", "is_enabled": 0, "is_locked": 0,
         "priority": 10, "param_defs": [], "params": {}},
    ]
    built = registry.build_all(metas)
    assert [a.code for a in built] == ["risk_guard"]       # 잠긴 알고리즘은 항상 포함
    built_all = registry.build_all(metas, enabled_only=False)
    assert [a.code for a in built_all] == ["risk_guard", "momentum_screen"]


def test_unknown_code_is_ignored():
    assert registry.build({"code": "존재하지않음", "param_defs": [], "params": {}}) is None


# ====================================================================== #
# momentum_screen
# ====================================================================== #
MOM_DEFS = defs(
    ("market", "enum", "000", "000:전체,001:코스피,101:코스닥"),
    ("min_flu_rt", "decimal", "3", None),
    ("max_flu_rt", "decimal", "15", None),
    ("min_volume_surge_rt", "decimal", "100", None),
    ("min_trde_qty", "int", "100000", None),
    ("min_price", "int", "1000", None),
    ("exclude_etf", "bool", "1", None),
    ("top_n", "int", "5", None),
    ("buy_amount", "int", "100000", None),
    ("max_new_per_day", "int", "3", None),
    ("order_type", "enum", "3", "3:시장가,0:지정가(보통)"),
)


def momentum(**over):
    return MomentumScreen(meta={"code": "momentum_screen"}, params=ParamSet(MOM_DEFS, over))


def rank(code, name, price, flu, qty=500000):
    return {"rank_no": 1, "stk_cd": code, "stk_nm": name, "cur_prc": price,
            "flu_rt": Decimal(str(flu)), "now_trde_qty": qty, "sdnin_rt": None}


def surge(code, name, price, flu, sdnin, qty=500000):
    return {"rank_no": 1, "stk_cd": code, "stk_nm": name, "cur_prc": price,
            "flu_rt": Decimal(str(flu)), "now_trde_qty": qty, "sdnin_rt": Decimal(str(sdnin))}


def test_momentum_intersection_only():
    market = FakeMarket(ranks=[rank("005930", "삼성전자", 60000, 5),
                               rank("000660", "SK하이닉스", 120000, 6)],
                        surges=[surge("005930", "삼성전자", 60000, 5, 300)])
    sigs = momentum().evaluate(make_ctx(FakeDb(), market=market))
    assert [s.stk_cd for s in sigs] == ["005930"]
    assert sigs[0].side == "BUY" and sigs[0].qty == 1          # 100,000 // 60,000


def test_momentum_records_screening_always():
    market = FakeMarket(ranks=[rank("005930", "삼성전자", 60000, 5)],
                        surges=[surge("005930", "삼성전자", 60000, 5, 300)])
    momentum().evaluate(make_ctx(FakeDb(), market=market))
    apis = {api for api, _ in market.recorded}
    assert apis == {"ka10027", "ka10023"}


def test_momentum_filters_out_of_range_rate():
    market = FakeMarket(ranks=[rank("005930", "삼성전자", 60000, 25)],
                        surges=[surge("005930", "삼성전자", 60000, 25, 300)])
    assert momentum().evaluate(make_ctx(FakeDb(), market=market)) == []


def test_momentum_filters_low_surge():
    market = FakeMarket(ranks=[rank("005930", "삼성전자", 60000, 5)],
                        surges=[surge("005930", "삼성전자", 60000, 5, 50)])
    assert momentum().evaluate(make_ctx(FakeDb(), market=market)) == []


def test_momentum_excludes_etf():
    market = FakeMarket(ranks=[rank("069500", "KODEX 200", 30000, 5)],
                        surges=[surge("069500", "KODEX 200", 30000, 5, 300)])
    assert momentum().evaluate(make_ctx(FakeDb(), market=market)) == []


def test_momentum_skips_held_stock():
    market = FakeMarket(ranks=[rank("005930", "삼성전자", 60000, 5)],
                        surges=[surge("005930", "삼성전자", 60000, 5, 300)])
    ctx = make_ctx(FakeDb(), market=market, holdings={"005930": {"rmnd_qty": 1}})
    assert momentum().evaluate(ctx) == []


def test_momentum_daily_entry_limit():
    db = FakeDb()
    db.new_entries_today = 3
    market = FakeMarket(ranks=[rank("005930", "삼성전자", 60000, 5)],
                        surges=[surge("005930", "삼성전자", 60000, 5, 300)])
    assert momentum().evaluate(make_ctx(db, market=market)) == []


def test_momentum_no_signal_when_market_closed():
    market = FakeMarket(ranks=[rank("005930", "삼성전자", 60000, 5)],
                        surges=[surge("005930", "삼성전자", 60000, 5, 300)])
    assert momentum().evaluate(make_ctx(FakeDb(), market=market, market_open=False)) == []


# ====================================================================== #
# volatility_breakout
# ====================================================================== #
VB_DEFS = defs(
    ("k_value", "decimal", "0.5", None),
    ("min_range_pct", "decimal", "1.5", None),
    ("watch_symbols", "string", "", None),
    ("buy_amount", "int", "100000", None),
    ("liquidate_time", "time", "15:15", None),
    ("order_type", "enum", "3", "3:시장가,0:지정가(보통)"),
)


def vbreak(**over):
    return VolatilityBreakout(meta={"code": "volatility_breakout"}, params=ParamSet(VB_DEFS, over))


def test_target_price_formula():
    assert target_price(10000, 10500, 9500, 0.5) == 10500
    assert target_price(10000, 10500, 9500, 1.0) == 11000


def bars(prev_high=10500, prev_low=9500, prev_close=10000):
    return [
        {"dt": _dt.date(2026, 9, 17), "open_pric": 9800, "high_pric": prev_high,
         "low_pric": prev_low, "cur_prc": prev_close},
        {"dt": _dt.date(2026, 9, 18), "open_pric": 10000, "high_pric": 10600,
         "low_pric": 9900, "cur_prc": 10600},
    ]


def test_breakout_triggers_above_target():
    market = FakeMarket(bars={"005930": bars()},
                        quotes={"005930": {"stk_nm": "삼성전자", "open_pric": 10000,
                                           "cur_prc": 10600}})
    sigs = vbreak(watch_symbols="005930", liquidate_time="").evaluate(make_ctx(FakeDb(), market=market))
    assert len(sigs) == 1 and sigs[0].side == "BUY"
    assert sigs[0].meta["target"] == 10500


def test_breakout_not_triggered_below_target():
    market = FakeMarket(bars={"005930": bars()},
                        quotes={"005930": {"stk_nm": "삼성전자", "open_pric": 10000,
                                           "cur_prc": 10100}})
    assert vbreak(watch_symbols="005930", liquidate_time="").evaluate(
        make_ctx(FakeDb(), market=market)) == []


def test_breakout_requires_min_range():
    market = FakeMarket(bars={"005930": bars(prev_high=10010, prev_low=10000)},
                        quotes={"005930": {"stk_nm": "x", "open_pric": 10000, "cur_prc": 99999}})
    assert vbreak(watch_symbols="005930", liquidate_time="").evaluate(
        make_ctx(FakeDb(), market=market)) == []


def test_liquidate_at_time():
    holdings = {"005930": {"stk_cd": "005930", "stk_nm": "삼성전자", "rmnd_qty": 5,
                           "trde_able_qty": 5}}
    positions = {"005930": {"entry_algo": "volatility_breakout"}}
    ctx = make_ctx(FakeDb(), market=FakeMarket(), holdings=holdings, positions=positions,
                   now=_dt.datetime(2026, 9, 18, 15, 16))
    sigs = vbreak(watch_symbols="005930").evaluate(ctx)
    assert len(sigs) == 1 and sigs[0].side == "SELL" and sigs[0].kind == "liquidate"


def test_no_liquidate_for_other_algo_position():
    holdings = {"005930": {"stk_cd": "005930", "rmnd_qty": 5, "trde_able_qty": 5}}
    positions = {"005930": {"entry_algo": "momentum_screen"}}
    ctx = make_ctx(FakeDb(), market=FakeMarket(), holdings=holdings, positions=positions,
                   now=_dt.datetime(2026, 9, 18, 15, 16))
    assert vbreak(watch_symbols="005930").evaluate(ctx) == []


# ====================================================================== #
# averaging_down
# ====================================================================== #
AD_DEFS = defs(
    ("drop_pct", "decimal", "10", None),
    ("step_buy_amount", "int", "100000", None),
    ("max_steps", "int", "3", None),
    ("cooldown_min", "int", "30", None),
    ("order_type", "enum", "3", "3:시장가,0:지정가(보통)"),
)


def avgdown(**over):
    return AveragingDown(meta={"code": "averaging_down"}, params=ParamSet(AD_DEFS, over))


def holding(cur=9000, pur=10000, qty=10, rate=-10):
    return {"005930": {"stk_cd": "005930", "stk_nm": "삼성전자", "rmnd_qty": qty,
                       "trde_able_qty": qty, "cur_prc": cur, "pur_pric": pur, "prft_rt": rate}}


def test_avg_down_triggers_on_drop():
    positions = {"005930": {"last_buy_price": 10000, "avg_down_count": 0,
                            "total_invested": 100000}}
    sigs = avgdown().evaluate(make_ctx(FakeDb(), holdings=holding(), positions=positions))
    assert len(sigs) == 1 and sigs[0].kind == "avg_down" and sigs[0].side == "BUY"


def test_avg_down_not_triggered_small_drop():
    positions = {"005930": {"last_buy_price": 10000, "avg_down_count": 0}}
    h = holding(cur=9500, rate=-5)
    assert avgdown().evaluate(make_ctx(FakeDb(), holdings=h, positions=positions)) == []


def test_avg_down_max_steps_blocks_infinite():
    """무한 물타기 방지: avg_down_count >= max_steps 이면 추가매수 신호 없음."""
    positions = {"005930": {"last_buy_price": 10000, "avg_down_count": 3}}
    assert avgdown(max_steps="3").evaluate(
        make_ctx(FakeDb(), holdings=holding(), positions=positions)) == []


def test_avg_down_zero_max_steps_disables():
    positions = {"005930": {"last_buy_price": 10000, "avg_down_count": 0}}
    assert avgdown(max_steps="0").evaluate(
        make_ctx(FakeDb(), holdings=holding(), positions=positions)) == []


def test_avg_down_cooldown():
    now = _dt.datetime(2026, 9, 18, 10, 30)
    positions = {"005930": {"last_buy_price": 10000, "avg_down_count": 1,
                            "updated_at": now - _dt.timedelta(minutes=5)}}
    assert avgdown().evaluate(
        make_ctx(FakeDb(), holdings=holding(), positions=positions, now=now)) == []


def test_avg_down_stop_loss_sells_all():
    positions = {"005930": {"last_buy_price": 10000, "avg_down_count": 1}}
    h = holding(cur=8000, rate=-20)
    sigs = avgdown().evaluate(make_ctx(FakeDb(), holdings=h, positions=positions))
    assert len(sigs) == 1 and sigs[0].side == "SELL" and sigs[0].kind == "stop_loss"
    assert sigs[0].qty == 10


def test_avg_down_no_signal_when_market_closed():
    positions = {"005930": {"last_buy_price": 10000, "avg_down_count": 0}}
    assert avgdown().evaluate(
        make_ctx(FakeDb(), holdings=holding(), positions=positions, market_open=False)) == []


# ====================================================================== #
# ma_cross_filter
# ====================================================================== #
MA_DEFS = defs(
    ("short_period", "int", "5", None),
    ("long_period", "int", "20", None),
    ("block_mode", "enum", "below", "below:하락국면 차단,cross_down:데드크로스만 차단"),
)


def ma_filter(**over):
    return MaCrossFilter(meta={"code": "ma_cross_filter"}, params=ParamSet(MA_DEFS, over))


def series(prices):
    base = _dt.date(2026, 1, 1)
    return [{"dt": base + _dt.timedelta(days=i), "cur_prc": p, "open_pric": p,
             "high_pric": p, "low_pric": p} for i, p in enumerate(prices)]


def test_moving_average():
    bars_ = series([10, 20, 30, 40, 50])
    assert moving_average(bars_, 5) == 30
    assert moving_average(bars_, 2) == 45
    assert moving_average(bars_, 2, offset=1) == 35
    assert moving_average(bars_, 10) is None


def test_filter_blocks_downtrend():
    prices = list(range(100, 70, -1))            # 하락 추세 30봉
    market = FakeMarket(bars={"005930": series(prices)})
    sig = Signal(algo_code="momentum_screen", stk_cd="005930", side="BUY", qty=1, price=100)
    out = ma_filter().filter_signals(make_ctx(FakeDb(), market=market), [sig])
    assert out == []


def test_filter_allows_uptrend():
    prices = list(range(70, 100))                # 상승 추세
    market = FakeMarket(bars={"005930": series(prices)})
    sig = Signal(algo_code="momentum_screen", stk_cd="005930", side="BUY", qty=1, price=100)
    out = ma_filter().filter_signals(make_ctx(FakeDb(), market=market), [sig])
    assert out == [sig]


def test_filter_never_blocks_sell():
    prices = list(range(100, 70, -1))
    market = FakeMarket(bars={"005930": series(prices)})
    sig = Signal(algo_code="risk_guard", stk_cd="005930", side="SELL", qty=1, kind="stop_loss")
    out = ma_filter().filter_signals(make_ctx(FakeDb(), market=market), [sig])
    assert out == [sig]


def test_filter_passes_when_insufficient_bars():
    market = FakeMarket(bars={"005930": series([100, 99, 98])})
    sig = Signal(algo_code="momentum_screen", stk_cd="005930", side="BUY", qty=1, price=100)
    assert ma_filter().filter_signals(make_ctx(FakeDb(), market=market), [sig]) == [sig]


def test_filter_disabled_when_short_ge_long():
    prices = list(range(100, 70, -1))
    market = FakeMarket(bars={"005930": series(prices)})
    sig = Signal(algo_code="momentum_screen", stk_cd="005930", side="BUY", qty=1, price=100)
    ctx = make_ctx(FakeDb(), market=market)
    assert ma_filter(short_period="20", long_period="5").filter_signals(ctx, [sig]) == [sig]
    assert any("필터 비활성" in n for n in ctx.notes)


@pytest.mark.parametrize("mode", ["below", "cross_down"])
def test_filter_modes_run(mode):
    prices = list(range(100, 70, -1))
    market = FakeMarket(bars={"005930": series(prices)})
    sig = Signal(algo_code="momentum_screen", stk_cd="005930", side="BUY", qty=1, price=100)
    ma_filter(block_mode=mode).filter_signals(make_ctx(FakeDb(), market=market), [sig])
