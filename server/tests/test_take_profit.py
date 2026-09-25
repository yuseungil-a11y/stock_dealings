"""take_profit(익절: ATR 트레일링 + 부분매도) 단위테스트."""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

import pytest

from conftest import FakeDb, FakeMarket, make_ctx
from stock_svr.algo.base import KIND_STOP_LOSS, KIND_TAKE_PROFIT
from stock_svr.algo.params import ParamSet
from stock_svr.algo.take_profit import TakeProfit, wilder_atr

TP_DEFS = [
    {"param_key": "partial_take_pct", "label": "부분익절 수익률", "value_type": "decimal",
     "default_value": "12", "enum_options": None},
    {"param_key": "partial_sell_ratio", "label": "부분매도 비율", "value_type": "decimal",
     "default_value": "40", "enum_options": None},
    {"param_key": "atr_period", "label": "ATR 기간", "value_type": "int",
     "default_value": "22", "enum_options": None},
    {"param_key": "atr_multiplier", "label": "ATR 배수", "value_type": "decimal",
     "default_value": "3.0", "enum_options": None},
    {"param_key": "min_atr_bars", "label": "ATR 최소 일봉 개수", "value_type": "int",
     "default_value": "23", "enum_options": None},
    {"param_key": "trail_activate_mode", "label": "트레일링 활성 시점", "value_type": "enum",
     "default_value": "after_partial",
     "enum_options": "after_partial:부분익절 이후,profit_pct:지정 수익률 도달 시"},
    {"param_key": "trail_activate_pct", "label": "트레일링 활성 수익률", "value_type": "decimal",
     "default_value": "12", "enum_options": None},
    {"param_key": "trail_floor_breakeven", "label": "트레일링 손익분기 하한", "value_type": "bool",
     "default_value": "1", "enum_options": None},
    {"param_key": "scope", "label": "적용 대상", "value_type": "enum", "default_value": "engine",
     "enum_options": "engine:시스템 진입 종목만,all:모든 보유종목"},
]


def take_profit(**over) -> TakeProfit:
    return TakeProfit(meta={"code": "take_profit"}, params=ParamSet(TP_DEFS, over))


def holding(cur=11200, pur=10000, qty=100, rate=12, trde_able=None):
    return {"005930": {"stk_cd": "005930", "stk_nm": "삼성전자", "rmnd_qty": qty,
                       "trde_able_qty": trde_able if trde_able is not None else qty,
                       "cur_prc": cur, "pur_pric": pur, "prft_rt": rate}}


ENGINE_POSITIONS = {"005930": {"entry_algo": "momentum_screen"}}


# ====================================================================== #
# Wilder ATR 순수함수
# ====================================================================== #
def test_wilder_atr_matches_hand_calculation():
    # TR1=TR2=TR3=3(첫 3개 평균), TR4=4 → ATR = (3*2+4)/3 = 10/3
    bars = [
        {"high_pric": 10, "low_pric": 8, "cur_prc": 9},
        {"high_pric": 12, "low_pric": 9, "cur_prc": 11},
        {"high_pric": 13, "low_pric": 10, "cur_prc": 12},
        {"high_pric": 11, "low_pric": 9, "cur_prc": 10},
        {"high_pric": 14, "low_pric": 10, "cur_prc": 13},
    ]
    atr = wilder_atr(bars, 3)
    assert atr is not None
    assert float(atr) == pytest.approx(10 / 3, abs=1e-9)


def test_wilder_atr_none_when_not_enough_bars():
    bars = [{"high_pric": 10, "low_pric": 8, "cur_prc": 9},
            {"high_pric": 12, "low_pric": 9, "cur_prc": 11}]
    assert wilder_atr(bars, 3) is None    # period+1=4개 필요, 2개뿐


# ====================================================================== #
# 1. 손절·익절 동시조건 - 손절이 항상 우선
# ====================================================================== #
def test_gapdown_stop_loss_wins_over_take_profit():
    """rt <= risk_guard.stop_loss_pct(기본 -15) 이면 이 알고리즘은 아무 신호도 내지 않는다."""
    h = holding(cur=8000, pur=10000, qty=10, rate=-20, trde_able=10)
    ctx = make_ctx(FakeDb(), market=FakeMarket(), holdings=h, positions=ENGINE_POSITIONS)
    assert take_profit().evaluate(ctx) == []


def test_gapdown_only_risk_guard_emits_stop_loss(ctx_factory):
    """같은 상황에서 risk_guard 는 정상적으로 KIND_STOP_LOSS 를 낸다(다른 알고리즘 임을 확인)."""
    from stock_svr.algo.risk_guard import RiskGuard

    guard_defs = [
        {"param_key": "stop_loss_pct", "label": "손절", "value_type": "decimal",
         "default_value": "-15", "enum_options": None},
    ]
    guard = RiskGuard(meta={"code": "risk_guard"}, params=ParamSet(guard_defs, {}))
    h = holding(cur=8000, pur=10000, qty=10, rate=-20, trde_able=10)
    ctx = ctx_factory(FakeDb(), holdings=h, positions=ENGINE_POSITIONS)
    sigs = guard.evaluate(ctx)
    assert len(sigs) == 1 and sigs[0].kind == KIND_STOP_LOSS


# ====================================================================== #
# 2. 부분 익절
# ====================================================================== #
def test_partial_take_fires_at_threshold_and_sells_correct_qty():
    db = FakeDb()
    h = holding(cur=11200, pur=10000, qty=100, rate=12, trde_able=100)   # rt=12% = 임계치
    ctx = make_ctx(db, market=FakeMarket(), holdings=h, positions=ENGINE_POSITIONS)
    sigs = take_profit().evaluate(ctx)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.side == "SELL" and sig.kind == KIND_TAKE_PROFIT
    assert sig.meta["stage"] == 1
    assert sig.qty == 40                       # 100주 x 40%

    # 알고리즘 자신은 peak/entry_pur 만 기록하고, tp_stage 는 아직 올리지 않는다
    # (실제 주문 전송 성공 후 Executor 가 올린다 - 관찰모드에선 절대 안 올라가야 함)
    st = db.get_position_exit_state(1, "005930")
    assert st is not None
    assert int(st.get("tp_stage") or 0) == 0
    assert st.get("peak_price") == 11200


def test_partial_take_below_threshold_no_signal():
    db = FakeDb()
    h = holding(cur=10500, pur=10000, qty=100, rate=5, trde_able=100)    # rt=5% < 12%
    ctx = make_ctx(db, market=FakeMarket(), holdings=h, positions=ENGINE_POSITIONS)
    assert take_profit().evaluate(ctx) == []


def test_partial_take_disabled_when_pct_zero():
    db = FakeDb()
    h = holding(cur=20000, pur=10000, qty=100, rate=100, trde_able=100)
    ctx = make_ctx(db, market=FakeMarket(), holdings=h, positions=ENGINE_POSITIONS)
    assert take_profit(partial_take_pct="0").evaluate(ctx) == []


def test_partial_take_tiny_qty_skips_sell_and_jumps_to_stage1():
    """가능수량이 1주뿐이면 부분매도를 하지 않고 매도 없이 stage=1 로 전환한다."""
    db = FakeDb()
    h = holding(cur=11200, pur=10000, qty=1, rate=12, trde_able=1)
    ctx = make_ctx(db, market=FakeMarket(), holdings=h, positions=ENGINE_POSITIONS)
    sigs = take_profit().evaluate(ctx)
    assert sigs == []                          # 매도 신호는 없다
    st = db.get_position_exit_state(1, "005930")
    assert st is not None and int(st["tp_stage"]) == 1
    assert st.get("tp_partial_qty") == 0


# ====================================================================== #
# 3. ATR 트레일링 청산
# ====================================================================== #
def _seed_trailing_state(db, *, peak=15000, atr="100", entry_pur=10000,
                         atr_date=_dt.date(2026, 9, 18)):
    db.position_exit_states[(1, "005930")] = {
        "tp_stage": 1, "tp_partial_qty": 40, "tp_partial_at": None,
        "peak_price": peak, "trail_started_at": None,
        "atr_value": Decimal(atr) if atr is not None else None,
        "atr_date": atr_date, "entry_pur_pric": entry_pur,
    }


def test_atr_trailing_exit_fires_when_price_breaks_stop():
    db = FakeDb()
    _seed_trailing_state(db, peak=15000, atr="100", entry_pur=10000)
    # stop = 15000 - 100*3(기본 배수) = 14700
    h = holding(cur=14600, pur=10000, qty=50, rate=46, trde_able=50)
    ctx = make_ctx(db, market=FakeMarket(), holdings=h, positions=ENGINE_POSITIONS,
                   now=_dt.datetime(2026, 9, 18, 10, 30))
    sigs = take_profit().evaluate(ctx)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.side == "SELL" and sig.meta["stage"] == 2 and sig.qty == 50


def test_atr_trailing_exit_not_triggered_above_stop():
    db = FakeDb()
    _seed_trailing_state(db, peak=15000, atr="100", entry_pur=10000)
    # stop = 14700, 현재가가 그보다 높으면 청산되지 않는다
    h = holding(cur=14800, pur=10000, qty=50, rate=48, trde_able=50)
    ctx = make_ctx(db, market=FakeMarket(), holdings=h, positions=ENGINE_POSITIONS,
                   now=_dt.datetime(2026, 9, 18, 10, 30))
    assert take_profit().evaluate(ctx) == []


def test_atr_trailing_respects_breakeven_floor():
    """트레일링 손익분기 하한(trail_floor_breakeven)이 켜져 있으면 평단 아래로 스탑이
    내려가지 않아(=스탑이 더 엄격해져) 바닥이 없을 때보다 더 일찍 청산된다."""
    db_on = FakeDb()
    _seed_trailing_state(db_on, peak=15000, atr="100", entry_pur=14800)
    # 바닥 없는 스탑 = 15000-300=14700. 평단(14800)이 더 높으므로 바닥 적용 시 14800.
    h = holding(cur=14750, pur=14800, qty=50, rate=0, trde_able=50)
    ctx_on = make_ctx(db_on, market=FakeMarket(), holdings=h, positions=ENGINE_POSITIONS,
                      now=_dt.datetime(2026, 9, 18, 10, 30))
    sigs_on = take_profit(trail_floor_breakeven="1").evaluate(ctx_on)
    assert len(sigs_on) == 1 and sigs_on[0].meta["stage"] == 2

    db_off = FakeDb()
    _seed_trailing_state(db_off, peak=15000, atr="100", entry_pur=14800)
    ctx_off = make_ctx(db_off, market=FakeMarket(), holdings=h, positions=ENGINE_POSITIONS,
                       now=_dt.datetime(2026, 9, 18, 10, 30))
    sigs_off = take_profit(trail_floor_breakeven="0").evaluate(ctx_off)
    assert sigs_off == []                      # 바닥이 없으면 14750 은 14700(스탑) 위라 청산 안됨


def test_trailing_not_active_before_partial_stage_in_after_partial_mode():
    """trail_activate_mode=after_partial(기본) 이면 stage=0 에서는 트레일링이 동작하지 않는다."""
    db = FakeDb()
    db.position_exit_states[(1, "005930")] = {
        "tp_stage": 0, "peak_price": 15000, "atr_value": Decimal("100"),
        "atr_date": _dt.date(2026, 9, 18), "entry_pur_pric": 10000}
    h = holding(cur=100, pur=10000, qty=50, rate=-99, trde_able=50)  # 큰 하락이지만 손절선 위
    # rate=-99 는 stop_loss_pct(-15)를 넘어버리므로 대신 완만한 하락으로 조정
    h["005930"]["prft_rt"] = -5
    h["005930"]["cur_prc"] = 9500
    ctx = make_ctx(db, market=FakeMarket(), holdings=h, positions=ENGINE_POSITIONS,
                   now=_dt.datetime(2026, 9, 18, 10, 30))
    assert take_profit().evaluate(ctx) == []


# ====================================================================== #
# 4. 같은 날 재진입 시 position_exit_state 리셋 (bump_position_invest 경로)
# ====================================================================== #
def test_exit_state_resets_on_same_day_reentry_after_full_exit():
    db = FakeDb()
    db.position_exit_states[(1, "005930")] = {"tp_stage": 2, "peak_price": 20000}
    db.bump_position_invest(1, "005930", 100000, "momentum_screen", 10000, avg_down=False)
    assert db.get_position_exit_state(1, "005930") is None


def test_exit_state_survives_averaging_down_buy():
    db = FakeDb()
    db.position_exit_states[(1, "005930")] = {"tp_stage": 1, "peak_price": 12000}
    db.bump_position_invest(1, "005930", 50000, "averaging_down", 9000, avg_down=True)
    assert db.get_position_exit_state(1, "005930") is not None


# ====================================================================== #
# 5. peak_price 갱신이 position_state.updated_at 을 건드리지 않는다 (회귀)
# ====================================================================== #
def test_peak_price_update_does_not_touch_position_state_updated_at():
    fixed_updated = _dt.datetime(2026, 9, 10, 9, 0)
    db = FakeDb()
    db.position_states[(1, "005930")] = {
        "entry_algo": "momentum_screen", "updated_at": fixed_updated,
        "avg_down_count": 0, "total_invested": 100000, "last_buy_price": 10000, "stopped": 0}
    h = holding(cur=10500, pur=10000, qty=10, rate=5, trde_able=10)
    ctx = make_ctx(db, market=FakeMarket(), holdings=h, positions=ENGINE_POSITIONS)
    take_profit().evaluate(ctx)
    assert db.position_states[(1, "005930")]["updated_at"] == fixed_updated
    st = db.get_position_exit_state(1, "005930")
    assert st is not None and st["peak_price"] == 10500


# ====================================================================== #
# 6. ATR 재계산 사이클당 상한
# ====================================================================== #
def _atr_bars(n=6, start_price=10000):
    base = _dt.date(2026, 9, 1)
    out = []
    price = start_price
    for i in range(n):
        out.append({"dt": base + _dt.timedelta(days=i), "open_pric": price,
                    "high_pric": price + 200, "low_pric": price - 200, "cur_prc": price})
        price += 50
    return out


def test_atr_refresh_capped_per_cycle():
    codes = [f"{i:06d}" for i in range(15)]
    holdings = {c: {"stk_cd": c, "stk_nm": c, "rmnd_qty": 10, "trde_able_qty": 10,
                    "cur_prc": 10500, "pur_pric": 10000, "prft_rt": 5} for c in codes}
    positions = {c: {"entry_algo": "momentum_screen"} for c in codes}
    db = FakeDb()
    for c in codes:
        db.position_exit_states[(1, c)] = {
            "tp_stage": 1, "peak_price": 10800, "atr_value": None, "atr_date": None,
            "entry_pur_pric": 10000}
    market = FakeMarket(bars={c: _atr_bars() for c in codes})
    ctx = make_ctx(db, market=market, holdings=holdings, positions=positions,
                   now=_dt.datetime(2026, 9, 18, 10, 30))
    take_profit(atr_period="3", min_atr_bars="4").evaluate(ctx)
    refreshed = sum(1 for c in codes if db.position_exit_states[(1, c)].get("atr_date") is not None)
    assert refreshed == 10


# ====================================================================== #
# 8. 거래불가 종목(R-06) / scope=engine 수동매수 제외
# ====================================================================== #
def test_untradable_stock_excluded():
    h = holding(cur=0, pur=10000, qty=10, rate=12, trde_able=10)   # 현재가 0 = 거래불가(R-06)
    ctx = make_ctx(FakeDb(), market=FakeMarket(), holdings=h, positions=ENGINE_POSITIONS)
    assert take_profit().evaluate(ctx) == []


def test_manual_buy_skipped_when_scope_engine():
    h = holding(cur=11200, pur=10000, qty=100, rate=12, trde_able=100)
    ctx = make_ctx(FakeDb(), market=FakeMarket(), holdings=h, positions={"005930": {}})
    assert take_profit().evaluate(ctx) == []       # scope=engine(기본) 이므로 수동매수 제외


def test_manual_buy_included_when_scope_all():
    h = holding(cur=11200, pur=10000, qty=100, rate=12, trde_able=100)
    ctx = make_ctx(FakeDb(), market=FakeMarket(), holdings=h, positions={"005930": {}})
    sigs = take_profit(scope="all").evaluate(ctx)
    assert len(sigs) == 1


def test_no_signal_when_market_closed():
    h = holding()
    ctx = make_ctx(FakeDb(), market=FakeMarket(), holdings=h, positions=ENGINE_POSITIONS,
                   market_open=False)
    assert take_profit().evaluate(ctx) == []


def test_no_signal_when_market_service_missing():
    h = holding()
    ctx = make_ctx(FakeDb(), market=None, holdings=h, positions=ENGINE_POSITIONS)
    assert take_profit().evaluate(ctx) == []


# ====================================================================== #
# validate_params
# ====================================================================== #
def test_validate_params_requires_min_bars_greater_than_period():
    errs = TakeProfit.validate_params(ParamSet(TP_DEFS, {"atr_period": "22", "min_atr_bars": "20"}))
    assert any("min_atr_bars" in e for e in errs)


def test_validate_params_rejects_contradictory_after_partial_with_zero_partial_pct():
    errs = TakeProfit.validate_params(ParamSet(TP_DEFS, {
        "trail_activate_mode": "after_partial", "partial_take_pct": "0"}))
    assert errs


def test_validate_params_ok_defaults():
    assert TakeProfit.validate_params(ParamSet(TP_DEFS, {})) == []
