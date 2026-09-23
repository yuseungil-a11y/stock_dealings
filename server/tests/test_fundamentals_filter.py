"""fundamentals_filter — PER·PBR·ROE·부채비율 개별 기준값 통과제 필터 테스트.

fake DB 만 사용하며 실 DB·실 DART API 를 호출하지 않는다. 매매 파이프라인(risk_guard/
Executor/게이트)에는 전혀 손대지 않고 `filter_signals` 만 검증한다.
"""
from __future__ import annotations

import ast
import datetime as _dt
import inspect
from decimal import Decimal

import pytest

from conftest import FakeDb, make_ctx
from stock_svr.algo import registry
from stock_svr.algo.base import (
    KIND_AVG_DOWN,
    KIND_ENTRY,
    KIND_LIQUIDATE,
    KIND_STOP_LOSS,
    KIND_TAKE_PROFIT,
    Signal,
)
from stock_svr.algo.fundamentals_filter import (
    CODE,
    FundamentalsFilter,
    FundamentalsOptions,
    age_days,
    check_indicators,
    check_valuation,
)
from stock_svr.algo.params import ParamSet

NOW = _dt.datetime(2026, 9, 23, 10, 30)     # 수요일 장중

# ---------------------------------------------------------------------- #
# seed.sql 의 algorithm_param_def 와 같은 정의
# ---------------------------------------------------------------------- #
DEFS = [
    {"param_key": "use_per", "label": "PER 상한 사용", "value_type": "bool", "default_value": "1"},
    {"param_key": "per_max", "label": "PER 상한", "value_type": "decimal", "default_value": "25",
     "min_value": "0", "max_value": "500"},
    {"param_key": "use_pbr", "label": "PBR 상한 사용", "value_type": "bool", "default_value": "1"},
    {"param_key": "pbr_max", "label": "PBR 상한", "value_type": "decimal", "default_value": "3",
     "min_value": "0", "max_value": "100"},
    {"param_key": "use_roe", "label": "ROE 하한 사용", "value_type": "bool", "default_value": "1"},
    {"param_key": "roe_min", "label": "ROE 하한", "value_type": "decimal", "default_value": "5",
     "min_value": "-100", "max_value": "200"},
    {"param_key": "use_debt", "label": "부채비율 상한 사용", "value_type": "bool", "default_value": "1"},
    {"param_key": "debt_ratio_max", "label": "부채비율 상한", "value_type": "decimal",
     "default_value": "200", "min_value": "0", "max_value": "2000"},
    {"param_key": "apply_to", "label": "적용 대상", "value_type": "enum", "default_value": "entry",
     "enum_options": "entry:신규 진입 매수만,entry_and_avg:신규 진입 + 물타기"},
    {"param_key": "stale_days", "label": "재무데이터 허용 경과일", "value_type": "int",
     "default_value": "10", "min_value": "1", "max_value": "90"},
]


def opts(**over) -> FundamentalsOptions:
    return FundamentalsOptions.from_params(ParamSet(DEFS, {k: str(v) for k, v in over.items()}))


def algo(**over) -> FundamentalsFilter:
    return FundamentalsFilter(meta={"code": CODE, "name": "재무필터"},
                              params=ParamSet(DEFS, {k: str(v) for k, v in over.items()}))


def buy(stk="005930", kind=KIND_ENTRY, price=None, meta=None) -> Signal:
    return Signal(algo_code="momentum_screen", stk_cd=stk, stk_nm="테스트", side="BUY",
                  qty=1, price=price, kind=kind, meta=meta or {})


def valuation(stk_cd="005930", dt=None, per="10", pbr="1", roe="10", debt="50") -> dict:
    """company_valuation_daily 행 흉내. None 을 넘기면 그 지표는 NULL(계산 불가)."""
    def d(v):
        return None if v is None else Decimal(str(v))
    return {"stk_cd": stk_cd, "dt": dt or NOW.date(), "cur_prc": 70_000,
            "eps_ttm": 5000, "bps": 60000, "per": d(per), "pbr": d(pbr),
            "roe": d(roe), "debt_ratio": d(debt), "financial_asof": "2026Q1"}


def db_with(row: dict | None, stk_cd="005930") -> FakeDb:
    db = FakeDb()
    if row is not None:
        db.valuations[(stk_cd, row["dt"])] = row
    return db


# ====================================================================== #
# 1. 레지스트리
# ====================================================================== #
def test_registered_and_known():
    assert registry.known_codes().__contains__(CODE)
    cls = registry.get(CODE)
    assert cls is FundamentalsFilter


def test_build_via_registry():
    meta = {"id": 1, "code": CODE, "name": "재무필터", "is_enabled": 1, "is_locked": 0,
            "priority": 38, "param_defs": DEFS, "params": {}}
    built = registry.build(meta)
    assert built is not None
    assert built.code == CODE
    assert built.role == "filter"


# ====================================================================== #
# 2. 지표별 정상 통과 / 개별 위반 차단
# ====================================================================== #
def test_all_pass():
    row = valuation(per="20", pbr="2", roe="8", debt="100")
    ok, reason = check_valuation(row, opts(), now=NOW)
    assert ok and reason == ""


def test_per_violation_blocks():
    row = valuation(per="32.1", pbr="1", roe="10", debt="50")
    ok, reason = check_valuation(row, opts(), now=NOW)
    assert not ok
    assert "PER" in reason and "32.1" in reason and "25" in reason


def test_pbr_violation_blocks():
    row = valuation(per="10", pbr="5.5", roe="10", debt="50")
    ok, reason = check_valuation(row, opts(), now=NOW)
    assert not ok
    assert "PBR" in reason and "5.5" in reason and "3" in reason


def test_roe_violation_blocks():
    row = valuation(per="10", pbr="1", roe="2.5", debt="50")
    ok, reason = check_valuation(row, opts(), now=NOW)
    assert not ok
    assert "ROE" in reason and "2.5" in reason and "5" in reason


def test_debt_violation_blocks():
    row = valuation(per="10", pbr="1", roe="10", debt="250")
    ok, reason = check_valuation(row, opts(), now=NOW)
    assert not ok
    assert "부채비율" in reason and "250" in reason and "200" in reason


def test_only_offending_indicator_named_per_first():
    """여러 지표가 동시에 위반이어도 PER→PBR→ROE→부채비율 순서로 첫 위반만 사유에 남는다."""
    row = valuation(per="99", pbr="99", roe="-99", debt="999")
    ok, reason = check_valuation(row, opts(), now=NOW)
    assert not ok
    assert "PER" in reason


# ====================================================================== #
# 3. 재무데이터 없음 / stale → 차단(fail-closed)
# ====================================================================== #
def test_no_data_blocks():
    ok, reason = check_valuation(None, opts(), now=NOW)
    assert not ok
    assert "재무데이터 없음" in reason


def test_stale_data_blocks():
    old_dt = (NOW - _dt.timedelta(days=15)).date()
    row = valuation(dt=old_dt)
    ok, reason = check_valuation(row, opts(stale_days=10), now=NOW)
    assert not ok
    assert "15일 전" in reason and "10일 초과" in reason


def test_fresh_within_stale_days_passes():
    ok_dt = (NOW - _dt.timedelta(days=5)).date()
    row = valuation(dt=ok_dt)
    ok, reason = check_valuation(row, opts(stale_days=10), now=NOW)
    assert ok


def test_exactly_at_stale_days_passes():
    """딱 stale_days 만큼 지난 경우는 초과가 아니므로 허용."""
    boundary_dt = (NOW - _dt.timedelta(days=10)).date()
    row = valuation(dt=boundary_dt)
    ok, reason = check_valuation(row, opts(stale_days=10), now=NOW)
    assert ok


# ====================================================================== #
# 4. NULL 지표값(계산 불가) → 차단
# ====================================================================== #
@pytest.mark.parametrize("field,label", [
    ("per", "PER"), ("pbr", "PBR"), ("roe", "ROE"), ("debt", "부채비율"),
])
def test_null_indicator_blocks(field, label):
    kwargs = {"per": "10", "pbr": "1", "roe": "10", "debt": "50"}
    kwargs[field] = None
    row = valuation(**kwargs)
    ok, reason = check_valuation(row, opts(), now=NOW)
    assert not ok
    assert label in reason and "계산 불가" in reason


def test_null_indicator_ignored_when_toggle_off():
    """토글이 꺼진 지표는 NULL 이어도 그 지표만으로는 차단하지 않는다."""
    row = valuation(per=None, pbr="1", roe="10", debt="50")
    ok, reason = check_valuation(row, opts(use_per=0), now=NOW)
    assert ok, reason


# ====================================================================== #
# 5. filter_signals — 매도·손절·청산은 절대 건드리지 않는다
# ====================================================================== #
@pytest.mark.parametrize("kind", [KIND_STOP_LOSS, KIND_LIQUIDATE, KIND_TAKE_PROFIT])
def test_sell_signals_never_touched_even_when_fail_closed(kind):
    """재무데이터가 아예 없어 fail-closed 상황이어도 SELL 은 그대로 통과한다."""
    db = db_with(None)
    ctx = make_ctx(db, now=NOW)
    sell = Signal(algo_code="risk_guard", stk_cd="005930", side="SELL", qty=10, kind=kind)
    kept = algo().filter_signals(ctx, [sell])
    assert kept == [sell]
    assert db.signals == []


def test_no_db_access_when_only_sell_signals():
    db = db_with(valuation())
    db.fail_on.add("latest_company_valuation")     # 호출되면 예외 → 조회하지 않음을 검증
    ctx = make_ctx(db, now=NOW)
    sell = Signal(algo_code="risk_guard", stk_cd="005930", side="SELL", qty=1,
                  kind=KIND_STOP_LOSS)
    assert algo().filter_signals(ctx, [sell]) == [sell]


def test_mixed_buy_and_sell_only_buy_is_checked():
    db = db_with(None)      # 재무데이터 없음 → 매수는 차단
    ctx = make_ctx(db, now=NOW)
    buy_sig = buy("005930")
    sell_sig = Signal(algo_code="risk_guard", stk_cd="000660", side="SELL", qty=1,
                      kind=KIND_STOP_LOSS)
    kept = algo().filter_signals(ctx, [buy_sig, sell_sig])
    assert kept == [sell_sig]


def test_filter_signals_source_never_mentions_sell_semantics_for_blocking():
    """AST 로 소스를 확인: SELL 신호를 차단하는 어떤 분기도 존재하지 않는다.

    `_applies` 는 `sig.side != "BUY"` 인 신호를 항상 관여 대상에서 제외하고,
    필터 로직 어디에도 `side == "SELL"` 을 검사해 차단하는 코드가 없어야 한다.
    """
    import stock_svr.algo.fundamentals_filter as mod

    src = inspect.getsource(mod)
    tree = ast.parse(src)
    found_sell_compare = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            dumped = ast.dump(node)
            if "'SELL'" in dumped or '"SELL"' in dumped:
                found_sell_compare = True
    assert not found_sell_compare, "SELL 을 직접 비교하는 코드가 있으면 안 된다(side!='BUY' 로만 걸러야 함)"


# ====================================================================== #
# 6. apply_to 매트릭스 (entry / entry_and_avg)
# ====================================================================== #
@pytest.mark.parametrize("apply_to,kind,blocked", [
    ("entry", KIND_ENTRY, True),
    ("entry", KIND_AVG_DOWN, False),
    ("entry_and_avg", KIND_ENTRY, True),
    ("entry_and_avg", KIND_AVG_DOWN, True),
])
def test_apply_to_matrix(apply_to, kind, blocked):
    db = db_with(None)     # 데이터 없음 → 걸리면 반드시 차단됨
    ctx = make_ctx(db, now=NOW)
    sig = buy("005930", kind=kind)
    kept = algo(apply_to=apply_to).filter_signals(ctx, [sig])
    assert (kept == []) is blocked


# ====================================================================== #
# 7. 모든 지표 토글 OFF → 전부 통과 + note 기록 (DB 조회 없음)
# ====================================================================== #
def test_all_toggles_off_passes_everything_without_db_access():
    db = db_with(None)
    db.fail_on.add("latest_company_valuation")
    ctx = make_ctx(db, now=NOW)
    sig = buy("005930")
    kept = algo(use_per=0, use_pbr=0, use_roe=0, use_debt=0).filter_signals(ctx, [sig])
    assert kept == [sig]
    assert any("비활성" in n for n in ctx.notes)


def test_any_enabled_property():
    assert opts().any_enabled is True
    assert opts(use_per=0, use_pbr=0, use_roe=0, use_debt=0).any_enabled is False
    assert opts(use_per=0, use_pbr=0, use_roe=0, use_debt=1).any_enabled is True


# ====================================================================== #
# 8. filter_signals 통합 (fake DB 경유)
# ====================================================================== #
def test_filter_signals_passes_when_valuation_ok():
    db = db_with(valuation(per="10", pbr="1", roe="10", debt="50"))
    ctx = make_ctx(db, now=NOW)
    sig = buy("005930")
    kept = algo().filter_signals(ctx, [sig])
    assert kept == [sig]
    assert db.signals == []


def test_filter_signals_blocks_and_logs_block_signal():
    db = db_with(valuation(per="99", pbr="1", roe="10", debt="50"))
    ctx = make_ctx(db, now=NOW)
    sig = buy("005930")
    kept = algo().filter_signals(ctx, [sig])
    assert kept == []
    assert len(db.signals) == 1
    rec = db.signals[0]
    assert rec["signal_type"] == "BLOCK" and rec["algo_code"] == CODE
    assert "PER" in rec["detail"] and sig.algo_code in rec["detail"]


def test_filter_signals_empty_input_returns_empty():
    ctx = make_ctx(db_with(None), now=NOW)
    assert algo().filter_signals(ctx, []) == []


def test_db_error_on_lookup_blocks_fail_closed():
    db = db_with(valuation())
    db.fail_on.add("latest_company_valuation")
    ctx = make_ctx(db, now=NOW)
    sig = buy("005930")
    kept = algo().filter_signals(ctx, [sig])
    assert kept == []
    assert "조회 실패" in db.signals[-1]["detail"]


# ====================================================================== #
# 9. age_days 헬퍼
# ====================================================================== #
def test_age_days_basic():
    assert age_days(NOW.date(), NOW) == 0
    assert age_days((NOW - _dt.timedelta(days=3)).date(), NOW) == 3


def test_age_days_none_when_not_a_date():
    assert age_days(None, NOW) is None
    assert age_days("2026-09-01", NOW) is None


# ====================================================================== #
# 10. seed.sql 기본값과 코드 기본값 일치
# ====================================================================== #
def test_seed_param_defs_match_options():
    o = FundamentalsOptions.from_params(ParamSet(DEFS, {}))
    assert (o.use_per, o.per_max) == (True, Decimal("25"))
    assert (o.use_pbr, o.pbr_max) == (True, Decimal("3"))
    assert (o.use_roe, o.roe_min) == (True, Decimal("5"))
    assert (o.use_debt, o.debt_ratio_max) == (True, Decimal("200"))
    assert (o.apply_to, o.stale_days) == ("entry", 10)


# ====================================================================== #
# 11. check_indicators 순서 보장 (단독 호출)
# ====================================================================== #
def test_check_indicators_direct():
    ok, _ = check_indicators(valuation(per="10", pbr="1", roe="10", debt="50"), opts())
    assert ok
    ok, reason = check_indicators(valuation(per="30", pbr="1", roe="10", debt="50"), opts())
    assert not ok and "PER" in reason
