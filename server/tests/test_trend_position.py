"""trend_position(중장기 추세추종 진입) / trend_position_exit(추세이탈·트레일링 청산) 테스트.

fake DB/시세만 사용하며 실 DB·실 API 를 호출하지 않는다.
take_profit 이 trend_position 진입 종목을 건드리지 않는지(회귀)도 여기서 확인한다.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from conftest import FakeDb, FakeMarket, make_ctx
from stock_svr.algo import registry
from stock_svr.algo.base import KIND_ENTRY, KIND_LIQUIDATE, KIND_TAKE_PROFIT
from stock_svr.algo.params import ParamSet
from stock_svr.algo.trend_position import TrendPosition, sma, trend_entry_state
from stock_svr.algo.trend_position_exit import TrendPositionExit, exit_reason, trailing_low
from test_audit_fixes import SqlSpyDb
from test_take_profit import holding as tp_holding
from test_take_profit import take_profit
from test_universe_filter import master


def defs(*items):
    return [{"param_key": k, "label": k, "value_type": t, "default_value": d,
             "enum_options": eo} for k, t, d, eo in items]


TP_ENTRY_DEFS = defs(
    ("ma_short_period", "int", "60", None),
    ("ma_long_period", "int", "120", None),
    ("confirm_days", "int", "3", None),
    ("buy_amount", "int", "400000", None),
    ("max_new_per_day", "int", "2", None),
    ("top_n", "int", "200", None),
    ("min_price", "int", "10000", None),
    ("max_price", "int", "0", None),
    ("min_market_cap_eok", "int", "0", None),
    ("use_kospi", "bool", "1", None),
    ("use_kosdaq", "bool", "1", None),
    ("use_etf", "bool", "0", None),
    ("exclude_preferred", "bool", "1", None),
    ("exclude_spac", "bool", "1", None),
    ("exclude_warning", "bool", "1", None),
    ("stale_days", "int", "5", None),
    ("order_type", "enum", "3", "3:시장가,0:지정가(보통),6:최유리지정가"),
)

EXIT_DEFS = defs(
    ("exit_ma_period", "int", "120", None),
    ("trail_lookback_days", "int", "10", None),
)

NOW = _dt.datetime(2026, 9, 18, 10, 30)  # 금요일 장중

# 단기 3 / 장기 6 / 확인 3 (필요 봉 = 6+3 = 9)
SHORT, LONG, CONFIRM = 3, 6, 3
UP_CLOSES = [10000 + 1000 * i for i in range(9)]           # 10000..18000 꾸준한 상승
DOWN_CLOSES = list(reversed(UP_CLOSES))                     # 하락 추세
# 상승 후 마지막 봉만 급락: MA3(16000,17000,13000 → 15333) > MA6(14666) 레짐은 유지되지만
# 마지막 종가 13000 < MA6 이라 "3일 연속 장기선 위" 확인 실패
DIP_CLOSES = [10000, 11000, 12000, 13000, 14000, 15000, 16000, 17000, 13000]


def entry_algo(**over) -> TrendPosition:
    base = {"ma_short_period": SHORT, "ma_long_period": LONG, "confirm_days": CONFIRM,
            "min_price": 0}
    base.update(over)
    return TrendPosition(meta={"code": "trend_position"}, params=ParamSet(TP_ENTRY_DEFS, base))


def exit_algo(**over) -> TrendPositionExit:
    return TrendPositionExit(meta={"code": "trend_position_exit"}, params=ParamSet(EXIT_DEFS, over))


def bars_from_closes(closes, lows=None, start=_dt.date(2026, 1, 1)):
    lows = lows or [c - 100 for c in closes]
    return [{"dt": start + _dt.timedelta(days=i), "cur_prc": c, "open_pric": c,
             "high_pric": c + 100, "low_pric": lo} for i, (c, lo) in enumerate(zip(closes, lows))]


def db_with_master(*codes) -> FakeDb:
    db = FakeDb()
    db.stock_master_rows = [master(c, f"종목{c}") for c in codes] or [master("005930", "삼성전자")]
    db.stock_master_updated_at = NOW
    return db


# ====================================================================== #
# 1. sma() / trend_entry_state() — 순수 계산
# ====================================================================== #
def test_sma_hand_calculation():
    assert sma([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_sma_period_one_is_identity():
    assert sma([7, 8, 9], 1) == [7.0, 8.0, 9.0]


def test_sma_insufficient_or_invalid_period_is_all_none():
    assert sma([1, 2], 3) == [None, None]
    assert sma([1, 2, 3], 0) == [None, None, None]


def test_trend_entry_state_uptrend_ok():
    st = trend_entry_state(UP_CLOSES, SHORT, LONG, CONFIRM)
    assert st is not None and st["ok"]
    assert st["ma_short"] == pytest.approx(17000)       # (16000+17000+18000)/3
    assert st["ma_long"] == pytest.approx(15500)        # (13000..18000)/6
    assert st["days_above"] == 3


def test_trend_entry_state_downtrend_regime_fails():
    st = trend_entry_state(DOWN_CLOSES, SHORT, LONG, CONFIRM)
    assert st is not None and not st["regime_ok"] and not st["ok"]


def test_trend_entry_state_dip_fails_confirmation_only():
    st = trend_entry_state(DIP_CLOSES, SHORT, LONG, CONFIRM)
    assert st is not None
    assert st["regime_ok"] is True
    assert st["confirm_ok"] is False and st["days_above"] == 2
    assert not st["ok"]


def test_trend_entry_state_invalid_inputs_none():
    assert trend_entry_state(UP_CLOSES, LONG, SHORT, CONFIRM) is None   # short >= long
    assert trend_entry_state(UP_CLOSES[:5], SHORT, LONG, CONFIRM) is None


# ====================================================================== #
# 2. 레지스트리 / 파라미터 검증
# ====================================================================== #
def test_registry_knows_new_algorithms():
    codes = registry.known_codes()
    assert "trend_position" in codes and "trend_position_exit" in codes
    assert registry.get("trend_position") is TrendPosition
    assert registry.get("trend_position_exit") is TrendPositionExit


def test_validate_params_short_ge_long_is_error():
    assert TrendPosition.validate_params(
        ParamSet(TP_ENTRY_DEFS, {"ma_short_period": "120", "ma_long_period": "120"}))
    assert TrendPosition.validate_params(ParamSet(TP_ENTRY_DEFS, {})) == []


def test_registry_build_disables_on_short_ge_long():
    meta = {"code": "trend_position", "role": "entry", "is_locked": 0,
            "param_defs": TP_ENTRY_DEFS, "params": {"ma_short_period": "130"}}
    assert registry.build(meta) is None


# ====================================================================== #
# 3. trend_position.evaluate()
# ====================================================================== #
def test_entry_signal_when_conditions_met():
    market = FakeMarket(bars={"005930": bars_from_closes(UP_CLOSES)})
    ctx = make_ctx(db_with_master("005930"), market=market, now=NOW)
    sigs = entry_algo().evaluate(ctx)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.side == "BUY" and s.kind == KIND_ENTRY and s.algo_code == "trend_position"
    assert s.qty == 400000 // 18000
    assert "MA3 17,000 > MA6 15,500" in s.reason and "3/3" in s.reason


def test_entry_signal_with_default_60_120_periods():
    closes = [10000 + 50 * i for i in range(130)]
    market = FakeMarket(bars={"005930": bars_from_closes(closes)})
    ctx = make_ctx(db_with_master("005930"), market=market, now=NOW)
    algo = TrendPosition(meta={"code": "trend_position"},
                         params=ParamSet(TP_ENTRY_DEFS, {"min_price": 0}))
    assert len(algo.evaluate(ctx)) == 1


def test_no_entry_when_regime_fails():
    market = FakeMarket(bars={"005930": bars_from_closes(DOWN_CLOSES)})
    ctx = make_ctx(db_with_master("005930"), market=market, now=NOW)
    assert entry_algo().evaluate(ctx) == []


def test_no_entry_when_not_three_days_above_long_ma():
    market = FakeMarket(bars={"005930": bars_from_closes(DIP_CLOSES)})
    ctx = make_ctx(db_with_master("005930"), market=market, now=NOW)
    assert entry_algo().evaluate(ctx) == []


def test_no_entry_when_already_held():
    market = FakeMarket(bars={"005930": bars_from_closes(UP_CLOSES)})
    held = {"005930": {"stk_cd": "005930", "stk_nm": "삼성전자", "rmnd_qty": 10,
                       "cur_prc": 18000, "pur_pric": 15000}}
    ctx = make_ctx(db_with_master("005930"), market=market, now=NOW, holdings=held)
    assert entry_algo().evaluate(ctx) == []


def test_no_entry_when_bars_insufficient():
    market = FakeMarket(bars={"005930": bars_from_closes(UP_CLOSES[:LONG + CONFIRM - 1])})
    ctx = make_ctx(db_with_master("005930"), market=market, now=NOW)
    assert entry_algo().evaluate(ctx) == []


def test_no_entry_when_market_closed():
    market = FakeMarket(bars={"005930": bars_from_closes(UP_CLOSES)})
    ctx = make_ctx(db_with_master("005930"), market=market, now=NOW, market_open=False)
    assert entry_algo().evaluate(ctx) == []


def test_entry_respects_daily_limit():
    db = db_with_master("005930")
    db.new_entries_today = 2
    market = FakeMarket(bars={"005930": bars_from_closes(UP_CLOSES)})
    ctx = make_ctx(db, market=market, now=NOW)
    assert entry_algo(max_new_per_day=2).evaluate(ctx) == []


# ====================================================================== #
# 4. trend_position_exit.evaluate()
# ====================================================================== #
# 확정 일봉 10개: 앞 7개 10000, 뒤 3개 12000 (저가 = 종가-100)
#   MA5 = (10000*2 + 12000*3)/5 = 11200, 최근 3거래일 최저 저가 = 11900
EXIT_CLOSES = [10000] * 7 + [12000] * 3


def exit_ctx(cur, entry="trend_position", bars=None, **kw):
    h = {"005930": {"stk_cd": "005930", "stk_nm": "삼성전자", "rmnd_qty": 20,
                    "trde_able_qty": 20, "cur_prc": cur, "pur_pric": 10000, "prft_rt": 0}}
    market = FakeMarket(bars={"005930": bars if bars is not None else bars_from_closes(EXIT_CLOSES)})
    return make_ctx(FakeDb(), market=market, holdings=h, now=NOW,
                    positions={"005930": {"entry_algo": entry}}, **kw)


def test_trailing_low_pure():
    bars = bars_from_closes([100, 200, 300], lows=[90, 150, 250])
    assert trailing_low(bars, 2) == 150
    assert trailing_low(bars, 4) is None


def test_exit_sells_all_below_long_ma():
    sigs = exit_algo(exit_ma_period=5, trail_lookback_days=3).evaluate(exit_ctx(11000))
    assert len(sigs) == 1
    s = sigs[0]
    assert s.side == "SELL" and s.kind == KIND_LIQUIDATE and s.qty == 20 and s.trde_tp == "3"
    assert "추세이탈" in s.reason and "MA5 11,200" in s.reason


def test_exit_sells_all_below_trailing_low():
    # 11500 >= MA5(11200) 이지만 최근 3거래일 최저 저가 11900 미만
    sigs = exit_algo(exit_ma_period=5, trail_lookback_days=3).evaluate(exit_ctx(11500))
    assert len(sigs) == 1
    assert sigs[0].kind == KIND_LIQUIDATE and sigs[0].qty == 20
    assert "트레일링" in sigs[0].reason and "11,900" in sigs[0].reason


def test_exit_no_signal_when_trend_intact():
    assert exit_algo(exit_ma_period=5, trail_lookback_days=3).evaluate(exit_ctx(12500)) == []


def test_exit_ignores_other_entry_algo():
    ctx = exit_ctx(11000, entry="momentum_screen")
    assert exit_algo(exit_ma_period=5, trail_lookback_days=3).evaluate(ctx) == []


def test_exit_ignores_manual_holding():
    ctx = exit_ctx(11000, entry=None)
    assert exit_algo(exit_ma_period=5, trail_lookback_days=3).evaluate(ctx) == []


def test_exit_excludes_today_incomplete_bar_from_trailing():
    # 당일(미확정) 봉의 저가 11000 이 포함되면 트레일링이 발동하지 않는다 - 제외돼야 한다
    bars = bars_from_closes(EXIT_CLOSES)
    bars.append({"dt": NOW.date(), "cur_prc": 11500, "open_pric": 12000,
                 "high_pric": 12000, "low_pric": 11000})
    sigs = exit_algo(exit_ma_period=5, trail_lookback_days=3).evaluate(exit_ctx(11500, bars=bars))
    assert len(sigs) == 1 and "트레일링" in sigs[0].reason


def test_exit_no_signal_when_bars_insufficient():
    bars = bars_from_closes([12000, 12000])
    assert exit_algo(exit_ma_period=5, trail_lookback_days=3).evaluate(
        exit_ctx(11000, bars=bars)) == []


def test_exit_no_signal_when_market_closed():
    ctx = exit_ctx(11000, market_open=False)
    assert exit_algo(exit_ma_period=5, trail_lookback_days=3).evaluate(ctx) == []


# ====================================================================== #
# 5. take_profit 회귀: trend_position 진입 종목은 take_profit 이 관리하지 않는다
# ====================================================================== #
def test_take_profit_skips_trend_position_holding():
    h = tp_holding(cur=11200, pur=10000, qty=100, rate=12)
    ctx = make_ctx(FakeDb(), market=FakeMarket(), holdings=h,
                   positions={"005930": {"entry_algo": "trend_position"}})
    assert take_profit().evaluate(ctx) == []


def test_take_profit_still_handles_other_entry_algo():
    h = tp_holding(cur=11200, pur=10000, qty=100, rate=12)
    ctx = make_ctx(FakeDb(), market=FakeMarket(), holdings=h,
                   positions={"005930": {"entry_algo": "momentum_screen"}})
    sigs = take_profit().evaluate(ctx)
    assert len(sigs) == 1 and sigs[0].kind == KIND_TAKE_PROFIT


def test_take_profit_skips_trend_position_even_with_scope_all():
    h = tp_holding(cur=11200, pur=10000, qty=100, rate=12)
    ctx = make_ctx(FakeDb(), market=FakeMarket(), holdings=h,
                   positions={"005930": {"entry_algo": "trend_position"}})
    assert take_profit(scope="all").evaluate(ctx) == []


# ====================================================================== #
# 6. H1: bump_position_invest() 의 entry_algo 갱신 규칙
#    신규 진입(avg_down=False) → 항상 새 값으로 덮어씀 / 물타기(avg_down=True) → 기존 값 유지
# ====================================================================== #
def _bump_sql(avg_down: bool) -> str:
    db = SqlSpyDb()
    db.bump_position_invest(1, "005930", 100000, "trend_position", 10000, avg_down=avg_down)
    sqls = [s for s, _ in db.sql if "INSERT INTO position_state" in s]
    assert len(sqls) == 1
    return sqls[0]


def test_bump_sql_new_entry_overwrites_entry_algo():
    sql = _bump_sql(avg_down=False)
    assert "entry_algo=VALUES(entry_algo)" in sql
    assert "COALESCE" not in sql


def test_bump_sql_avg_down_keeps_existing_entry_algo():
    sql = _bump_sql(avg_down=True)
    assert "entry_algo=COALESCE(entry_algo, VALUES(entry_algo))" in sql
    assert "entry_algo=VALUES(entry_algo)" not in sql


def test_fake_bump_new_entry_overwrites_previous_entry_algo():
    # momentum_screen 으로 샀다 전량매도한 종목을 trend_position 이 다시 사면 entry_algo 가 바뀐다
    db = FakeDb()
    db.bump_position_invest(1, "005930", 100000, "momentum_screen", 10000, avg_down=False)
    db.bump_position_invest(1, "005930", 150000, "trend_position", 12000, avg_down=False)
    assert db.position_states[(1, "005930")]["entry_algo"] == "trend_position"
    # 역방향도 동일
    db.bump_position_invest(1, "005930", 100000, "momentum_screen", 11000, avg_down=False)
    assert db.position_states[(1, "005930")]["entry_algo"] == "momentum_screen"


def test_fake_bump_avg_down_keeps_entry_algo():
    db = FakeDb()
    db.bump_position_invest(1, "005930", 150000, "trend_position", 12000, avg_down=False)
    db.bump_position_invest(1, "005930", 50000, "averaging_down", 11000, avg_down=True)
    st = db.position_states[(1, "005930")]
    assert st["entry_algo"] == "trend_position" and st["avg_down_count"] == 1


# ====================================================================== #
# 7. H2: exit_reason() 순수함수 경계값 + 진입 안전판(눌림목 거부)
# ====================================================================== #
# 종가 앞 7개 10000, 뒤 3개 12000 / 저가 = 종가-100 → MA5 = 11200, 최근 3봉 최저 저가 = 11900
_ER_CLOSES = [10000] * 7 + [12000] * 3
_ER_LOWS = [c - 100 for c in _ER_CLOSES]


def test_exit_reason_equal_to_ma_does_not_sell():
    lows = [100] * 10  # 트레일링이 걸리지 않게 저가를 낮게
    assert exit_reason(_ER_CLOSES, lows, 11200, 5, 3) is None
    r = exit_reason(_ER_CLOSES, lows, 11199, 5, 3)
    assert r is not None and "추세이탈" in r


def test_exit_reason_equal_to_trailing_low_does_not_sell():
    assert exit_reason(_ER_CLOSES, _ER_LOWS, 11900, 5, 3) is None
    r = exit_reason(_ER_CLOSES, _ER_LOWS, 11899, 5, 3)
    assert r is not None and "트레일링" in r and "11,900" in r


def test_exit_reason_none_when_intact_or_data_insufficient():
    assert exit_reason(_ER_CLOSES, _ER_LOWS, 12500, 5, 3) is None
    assert exit_reason([12000, 12000], [11900, 11900], 1000, 5, 3) is None
    assert exit_reason(_ER_CLOSES, _ER_LOWS, 0, 5, 3) is None


def _exit_row(exit_ma: int, lookback: int) -> dict:
    return {"id": 99, "code": "trend_position_exit", "role": "risk", "is_enabled": 0,
            "param_defs": EXIT_DEFS,
            "params": {"exit_ma_period": str(exit_ma), "trail_lookback_days": str(lookback)}}


def _bars_with_today(today_close: int) -> list[dict]:
    # 확정 일봉 UP_CLOSES(저가=종가-100) + 당일(미확정) 봉: 현재가 = today_close
    bars = bars_from_closes(UP_CLOSES)
    bars.append({"dt": NOW.date(), "cur_prc": today_close, "open_pric": today_close,
                 "high_pric": today_close, "low_pric": today_close})
    return bars


def test_no_entry_on_pullback_that_exit_would_sell_immediately():
    # 진입조건 충족: 마지막 9봉(당일 16800 포함) MA3 17267 > MA6 16133, 최근 3봉 모두 MA6 위
    # 하지만 청산 기준(확정 일봉만) 최근 2거래일 최저 저가 16900 > 현재가 16800 → 트레일링 매도 대상
    bars = _bars_with_today(16800)
    closes = [b["cur_prc"] for b in bars][-(LONG + CONFIRM):]
    assert trend_entry_state(closes, SHORT, LONG, CONFIRM)["ok"]  # 진입조건 자체는 충족
    confirmed = bars[:-1]
    assert exit_reason([b["cur_prc"] for b in confirmed], [b["low_pric"] for b in confirmed],
                       16800, 5, 2) is not None
    db = db_with_master("005930")
    db.algorithms = [_exit_row(5, 2)]
    ctx = make_ctx(db, market=FakeMarket(bars={"005930": bars}), now=NOW)
    assert entry_algo().evaluate(ctx) == []


def test_entry_still_signals_when_exit_reason_is_none():
    # 회귀: 같은 청산 파라미터라도 현재가 18500 이면 MA5 16000·최근 2일 최저 16900 위 → 정상 진입
    bars = _bars_with_today(18500)
    db = db_with_master("005930")
    db.algorithms = [_exit_row(5, 2)]
    ctx = make_ctx(db, market=FakeMarket(bars={"005930": bars}), now=NOW)
    sigs = entry_algo().evaluate(ctx)
    assert len(sigs) == 1 and sigs[0].side == "BUY" and sigs[0].kind == KIND_ENTRY


def test_entry_guard_uses_confirmed_bars_only():
    # 당일 미확정 봉(저가 16800)이 포함됐다면 최근 2봉 최저가가 16800 이 돼 매도 판정이 안 나온다.
    # 확정 봉만 쓰므로 최근 2거래일 최저 저가 16900 > 현재가 16800 → 진입 보류여야 한다(청산과 동일 기준).
    bars = _bars_with_today(16800)
    with_today = [b["low_pric"] for b in bars]
    assert exit_reason([b["cur_prc"] for b in bars], with_today, 16800, 5, 2) is None  # 포함 시 미발동
    db = db_with_master("005930")
    db.algorithms = [_exit_row(5, 2)]
    ctx = make_ctx(db, market=FakeMarket(bars={"005930": bars}), now=NOW)
    assert entry_algo().evaluate(ctx) == []
