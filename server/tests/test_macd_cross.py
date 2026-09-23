"""macd_cross — EMA/MACD 순수계산·골든크로스 판정·universe_filter 재사용 진입 신호 테스트.

fake DB/시세만 사용하며 실 DB·실 API 를 호출하지 않는다(단, seed.sql 적용 결과의 DB 확인은
별도로 수행 — 이 파일은 순수 로직 + FakeDb/FakeMarket 통합 테스트만 다룬다).
"""
from __future__ import annotations

import datetime as _dt

import pytest

from conftest import FakeDb, FakeMarket, make_ctx
from stock_svr.algo import registry
from stock_svr.algo.base import KIND_ENTRY
from stock_svr.algo.macd_cross import MacdCross, ema, golden_cross, macd_series
from stock_svr.algo.params import ParamSet
from test_universe_filter import master

# ====================================================================== #
# 손계산으로 검증한 고정 데이터
# ====================================================================== #
# fast=3, slow=6, signal=3 (need = slow+signal = 9)
FAST, SLOW, SIGNAL = 3, 6, 3

# 급락(가속) 후 반등 → MACD 가 Signal 을 마지막 봉에서 아래→위로 교차(골든크로스)한다.
# (server/tests 작성 시 `stock_svr.algo.macd_cross.macd_series/golden_cross` 로 직접 계산해
#  손으로 확인한 값. 아래 test_macd_series_matches_hand_calculation 에서 그 수치를 고정한다.)
GOLD_CLOSES = [100, 99, 97, 94, 90, 85, 79, 72, 64, 55, 70]
# GOLD_CLOSES 와 같은 모양이지만 등락폭이 2배 커서 골든크로스 시점의 히스토그램이 더 크다.
GOLD_BIG_CLOSES = [100, 98, 94, 88, 80, 70, 58, 44, 28, 10, 40]
# 마지막 반등 봉이 없어(아직 하락 중) 교차가 일어나지 않는다.
NO_CROSS_CLOSES = GOLD_CLOSES[:-1]


def defs(*items):
    return [{"param_key": k, "label": k, "value_type": t, "default_value": d,
             "enum_options": eo} for k, t, d, eo in items]


MACD_DEFS = defs(
    ("fast_period", "int", "12", None),
    ("slow_period", "int", "26", None),
    ("signal_period", "int", "9", None),
    ("top_n", "int", "100", None),
    ("use_kospi", "bool", "1", None),
    ("use_kosdaq", "bool", "1", None),
    ("use_etf", "bool", "0", None),
    ("min_price", "int", "10000", None),
    ("max_price", "int", "0", None),
    ("min_market_cap_eok", "int", "0", None),
    ("exclude_preferred", "bool", "1", None),
    ("exclude_spac", "bool", "1", None),
    ("exclude_warning", "bool", "1", None),
    ("stale_days", "int", "5", None),
    ("buy_amount", "int", "100000", None),
    ("max_new_per_day", "int", "3", None),
    ("order_type", "enum", "3", "3:시장가,0:지정가(보통),6:최유리지정가"),
)


def macd_algo(**over) -> MacdCross:
    return MacdCross(meta={"code": "macd_cross"}, params=ParamSet(MACD_DEFS, over))


def bars_from_closes(closes: list[float]) -> list[dict]:
    base = _dt.date(2026, 1, 1)
    return [{"dt": base + _dt.timedelta(days=i), "cur_prc": c, "open_pric": c,
             "high_pric": c, "low_pric": c} for i, c in enumerate(closes)]


def db_with(rows, updated_at) -> FakeDb:
    db = FakeDb()
    db.stock_master_rows = list(rows)
    db.stock_master_updated_at = updated_at
    return db


NOW = _dt.datetime(2026, 9, 18, 10, 30)  # 금요일 장중


# ====================================================================== #
# 1. ema() — 순수 계산
# ====================================================================== #
def test_ema_constant_sequence_equals_constant():
    """상수 시퀀스의 EMA는 그 상수와 같아야 한다. 손계산: SMA([5,5,5])=5, 이후도 5 유지."""
    out = ema([5.0] * 6, 3)
    assert out == [None, None, 5.0, 5.0, 5.0, 5.0]


def test_ema_insufficient_data_is_all_none():
    """기간보다 데이터가 적으면 전부 None."""
    assert ema([1.0, 2.0], 5) == [None, None]


def test_ema_step_function_converges_upward_without_overshoot():
    """0 이 5개, 그 다음 10 이 10개인 계단형 시퀀스, period=3.

    손계산: k=2/(3+1)=0.5. SMA([0,0,0])=0 (index2).
    전환 직후(index5, 값=10): prev*0.5 + 10*0.5 = 0*0.5+10*0.5 = 5.0.
    이후 단조증가하며 10을 넘지 않고 10에 수렴해야 한다.
    """
    out = ema([0.0] * 5 + [10.0] * 10, 3)
    assert out[:2] == [None, None]
    assert out[2] == 0.0
    assert out[5] == 5.0                      # 전환 직후 손계산값
    tail = out[5:]
    assert all(v is not None for v in tail)
    # 단조증가 & 10을 넘지 않음(수렴 방향 확인)
    for prev, cur in zip(tail, tail[1:]):
        assert prev <= cur <= 10.0
    assert tail[-1] > 9.9                     # 충분히 수렴


# ====================================================================== #
# 2. macd_series() — 순수 계산
# ====================================================================== #
def test_macd_series_insufficient_bars_returns_empty():
    """slow+signal(=9) 미만 봉이면 빈 리스트."""
    closes = list(range(8))                  # 8개 < 9
    macd, signal = macd_series(closes, FAST, SLOW, SIGNAL)
    assert macd == [] and signal == []


def test_macd_series_matches_hand_calculation():
    """GOLD_CLOSES(11봉)로 손계산한 macd/signal 값 검증(직접 계산해 고정한 수치)."""
    macd, signal = macd_series(GOLD_CLOSES, FAST, SLOW, SIGNAL)
    assert len(macd) == len(GOLD_CLOSES) == len(signal)
    # slow=6 → macd 는 index 5부터 값이 있다 (앞 5개 None)
    assert macd[:5] == [None] * 5
    assert macd[5] == pytest.approx(-5.083333333333329)
    # signal 은 macd 가 유효해진 뒤로 signal_period(3)개가 더 필요 → index 7부터 값
    assert signal[:7] == [None] * 7
    assert signal[7] == pytest.approx(-5.8640873015873)
    # 마지막 봉(반등)에서 교차
    assert macd[-1] == pytest.approx(-4.932561736429264)
    assert signal[-1] == pytest.approx(-6.425611326588456)


def test_macd_series_zero_or_negative_period_returns_empty():
    assert macd_series([1.0] * 20, 0, 26, 9) == ([], [])
    assert macd_series([1.0] * 20, 12, 26, -1) == ([], [])


# ====================================================================== #
# 3. golden_cross() — 순수 판정
# ====================================================================== #
def test_golden_cross_true_when_crossing_up():
    macd = [None, -1.0, 1.0]
    signal = [None, 0.0, 0.0]
    assert golden_cross(macd, signal) is True


def test_golden_cross_false_when_already_above():
    """어제도 이미 위였다면 '새 교차'가 아니다."""
    macd = [None, 2.0, 3.0]
    signal = [None, 1.0, 1.0]
    assert golden_cross(macd, signal) is False


def test_golden_cross_false_when_still_below():
    macd = [None, -2.0, -1.0]
    signal = [None, 0.0, 0.0]
    assert golden_cross(macd, signal) is False


def test_golden_cross_false_when_none_present():
    macd = [None, None, 1.0]
    signal = [None, 0.0, 0.0]
    assert golden_cross(macd, signal) is False


def test_golden_cross_false_when_too_short():
    assert golden_cross([1.0], [1.0]) is False
    assert golden_cross([], []) is False


def test_golden_cross_matches_gold_closes_fixture():
    """실제 시나리오 데이터(GOLD_CLOSES)는 골든크로스, NO_CROSS_CLOSES는 아니다."""
    macd, signal = macd_series(GOLD_CLOSES, FAST, SLOW, SIGNAL)
    assert golden_cross(macd, signal) is True
    macd2, signal2 = macd_series(NO_CROSS_CLOSES, FAST, SLOW, SIGNAL)
    assert golden_cross(macd2, signal2) is False


# ====================================================================== #
# 4. validate_params
# ====================================================================== #
def test_validate_params_fast_ge_slow_is_error():
    errs = MacdCross.validate_params(ParamSet(MACD_DEFS, {"fast_period": "26", "slow_period": "12"}))
    assert errs and "단기" in errs[0] and "장기" in errs[0]


def test_validate_params_fast_lt_slow_is_ok():
    assert MacdCross.validate_params(
        ParamSet(MACD_DEFS, {"fast_period": "12", "slow_period": "26"})) == []


def test_validate_params_equal_periods_is_error():
    errs = MacdCross.validate_params(ParamSet(MACD_DEFS, {"fast_period": "12", "slow_period": "12"}))
    assert errs


# ====================================================================== #
# 5. 레지스트리
# ====================================================================== #
def test_registry_knows_macd_cross():
    assert "macd_cross" in registry.known_codes()
    assert registry.get("macd_cross") is MacdCross


def test_registry_builds_macd_cross_instance():
    meta = {"code": "macd_cross", "name": "MACD 골든크로스", "role": "entry", "is_locked": 0,
            "param_defs": MACD_DEFS, "params": {}}
    algo = registry.build(meta)
    assert isinstance(algo, MacdCross)
    assert algo.role == "entry"


# ====================================================================== #
# 6. evaluate() — 통합 (universe_filter 재사용 + 골든크로스 진입)
# ====================================================================== #
def test_evaluate_no_signal_when_market_closed():
    market = FakeMarket(bars={"005930": bars_from_closes(GOLD_CLOSES)})
    db = db_with([master("005930", "삼성전자")], NOW)
    ctx = make_ctx(db, market=market, market_open=False, now=NOW)
    assert macd_algo(fast_period=FAST, slow_period=SLOW, signal_period=SIGNAL,
                     min_price=0).evaluate(ctx) == []


def test_evaluate_no_signal_when_market_is_none():
    db = db_with([master("005930", "삼성전자")], NOW)
    ctx = make_ctx(db, market=None, now=NOW)
    assert macd_algo(fast_period=FAST, slow_period=SLOW, signal_period=SIGNAL,
                     min_price=0).evaluate(ctx) == []


def test_evaluate_universe_load_failure_notes_and_returns_empty():
    """종목마스터가 비어 있으면(fail-closed) 신호 없음 + note 기록."""
    market = FakeMarket(bars={"005930": bars_from_closes(GOLD_CLOSES)})
    db = db_with([], None)
    ctx = make_ctx(db, market=market, now=NOW)
    sigs = macd_algo(fast_period=FAST, slow_period=SLOW, signal_period=SIGNAL,
                     min_price=0).evaluate(ctx)
    assert sigs == []
    assert any("비어 있음" in n for n in ctx.notes)


def test_evaluate_stale_master_blocks():
    old = NOW - _dt.timedelta(days=20)
    market = FakeMarket(bars={"005930": bars_from_closes(GOLD_CLOSES)})
    db = db_with([master("005930", "삼성전자")], old)
    ctx = make_ctx(db, market=market, now=NOW)
    sigs = macd_algo(fast_period=FAST, slow_period=SLOW, signal_period=SIGNAL,
                     min_price=0, stale_days=5).evaluate(ctx)
    assert sigs == []
    assert any("갱신" in n for n in ctx.notes)


def test_evaluate_skips_already_held_stock():
    market = FakeMarket(bars={"005930": bars_from_closes(GOLD_CLOSES)})
    db = db_with([master("005930", "삼성전자")], NOW)
    ctx = make_ctx(db, market=market, now=NOW, holdings={"005930": {"rmnd_qty": 1}})
    sigs = macd_algo(fast_period=FAST, slow_period=SLOW, signal_period=SIGNAL,
                     min_price=0).evaluate(ctx)
    assert sigs == []


def test_evaluate_skips_untradable_stock():
    market = FakeMarket(bars={"005930": bars_from_closes(GOLD_CLOSES)})
    db = db_with([master("005930", "삼성전자")], NOW)
    db.stock_states["005930"] = "거래정지"
    ctx = make_ctx(db, market=market, now=NOW)
    sigs = macd_algo(fast_period=FAST, slow_period=SLOW, signal_period=SIGNAL,
                     min_price=0).evaluate(ctx)
    assert sigs == []


def test_evaluate_daily_entry_limit_blocks():
    market = FakeMarket(bars={"005930": bars_from_closes(GOLD_CLOSES)})
    db = db_with([master("005930", "삼성전자")], NOW)
    db.new_entries_today = 3
    ctx = make_ctx(db, market=market, now=NOW)
    sigs = macd_algo(fast_period=FAST, slow_period=SLOW, signal_period=SIGNAL,
                     min_price=0, max_new_per_day=3).evaluate(ctx)
    assert sigs == []
    assert any("한도 도달" in n for n in ctx.notes)


def test_evaluate_no_signal_without_golden_cross():
    market = FakeMarket(bars={"005930": bars_from_closes(NO_CROSS_CLOSES)})
    db = db_with([master("005930", "삼성전자")], NOW)
    ctx = make_ctx(db, market=market, now=NOW)
    sigs = macd_algo(fast_period=FAST, slow_period=SLOW, signal_period=SIGNAL,
                     min_price=0).evaluate(ctx)
    assert sigs == []


def test_evaluate_golden_cross_produces_buy_signal():
    market = FakeMarket(bars={"005930": bars_from_closes(GOLD_CLOSES)})
    db = db_with([master("005930", "삼성전자")], NOW)
    ctx = make_ctx(db, market=market, now=NOW)
    sigs = macd_algo(fast_period=FAST, slow_period=SLOW, signal_period=SIGNAL,
                     min_price=0, buy_amount=100000).evaluate(ctx)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.side == "BUY" and sig.kind == KIND_ENTRY and sig.qty > 0
    assert sig.stk_cd == "005930" and sig.algo_code == "macd_cross"
    assert "MACD 골든크로스" in sig.reason


def test_evaluate_excludes_stock_outside_universe_top_n():
    """universe_filter 와 같은 시총 상위 N 로직 재사용 — top_n 밖 종목은 골든크로스라도 제외."""
    rows = [
        master("005930", "삼성전자", "0", 1_000_000, 100_000),   # 시총 1위(1000억)
        master("000660", "SK하이닉스", "0", 100_000, 100_000),   # 시총 2위(100억)
    ]
    market = FakeMarket(bars={"005930": bars_from_closes(GOLD_CLOSES),
                              "000660": bars_from_closes(GOLD_BIG_CLOSES)})
    db = db_with(rows, NOW)
    ctx = make_ctx(db, market=market, now=NOW)
    sigs = macd_algo(fast_period=FAST, slow_period=SLOW, signal_period=SIGNAL,
                     min_price=0, top_n=1).evaluate(ctx)
    assert [s.stk_cd for s in sigs] == ["005930"]


def test_evaluate_trims_to_daily_limit_by_histogram_desc():
    """후보가 한도보다 많으면 히스토그램(|MACD-Signal|) 큰 순으로만 신호를 만든다."""
    rows = [
        master("005930", "삼성전자", "0", 1_000_000, 100_000),
        master("000660", "SK하이닉스", "10", 1_000_000, 100_000),
    ]
    market = FakeMarket(bars={"005930": bars_from_closes(GOLD_CLOSES),        # 히스토그램 작음
                              "000660": bars_from_closes(GOLD_BIG_CLOSES)})   # 히스토그램 큼
    db = db_with(rows, NOW)
    ctx = make_ctx(db, market=market, now=NOW)
    sigs = macd_algo(fast_period=FAST, slow_period=SLOW, signal_period=SIGNAL,
                     min_price=0, top_n=10, max_new_per_day=1).evaluate(ctx)
    assert [s.stk_cd for s in sigs] == ["000660"]      # 히스토그램 더 큰 쪽만 선택


def test_evaluate_qty_zero_is_skipped():
    """매수금액으로 1주도 못 사면 신호를 만들지 않는다."""
    market = FakeMarket(bars={"005930": bars_from_closes(GOLD_CLOSES)})
    db = db_with([master("005930", "삼성전자")], NOW)
    ctx = make_ctx(db, market=market, now=NOW)
    sigs = macd_algo(fast_period=FAST, slow_period=SLOW, signal_period=SIGNAL,
                     min_price=0, buy_amount=1).evaluate(ctx)
    assert sigs == []


def test_evaluate_falls_back_to_db_recent_bars_when_market_bars_empty():
    """FakeMarket 에 봉이 없으면(빈 리스트) ctx.db.recent_bars() 로 대체한다."""
    market = FakeMarket(bars={})   # 시세 서비스는 빈 결과
    db = db_with([master("005930", "삼성전자")], NOW)
    db.bars["005930"] = bars_from_closes(GOLD_CLOSES)
    ctx = make_ctx(db, market=market, now=NOW)
    sigs = macd_algo(fast_period=FAST, slow_period=SLOW, signal_period=SIGNAL,
                     min_price=0).evaluate(ctx)
    assert len(sigs) == 1 and sigs[0].stk_cd == "005930"


def test_evaluate_disabled_when_fast_ge_slow():
    market = FakeMarket(bars={"005930": bars_from_closes(GOLD_CLOSES)})
    db = db_with([master("005930", "삼성전자")], NOW)
    ctx = make_ctx(db, market=market, now=NOW)
    sigs = macd_algo(fast_period=26, slow_period=12, signal_period=SIGNAL,
                     min_price=0).evaluate(ctx)
    assert sigs == []
    assert any("알고리즘 비활성" in n for n in ctx.notes)
