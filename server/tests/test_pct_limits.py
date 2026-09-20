"""총자산 대비 비중(%) 투입 한도 테스트.

유효 한도 = min(절대한도(원), 총자산 × 비중%).
총자산을 알 수 없거나 0 이거나 스냅샷이 오래되면 BUY 차단(fail-closed), SELL 은 영향 없음.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

import pytest

from conftest import FakeDb, make_ctx
from stock_svr.algo.base import Signal
from stock_svr.algo.params import ParamError, ParamSet, validate
from stock_svr.algo.risk_guard import RiskGuard, effective_limit
from stock_svr.engine.context import BALANCE_STALE_SEC
from tests_support import RISK_DEFS, buy_signal

NOW = _dt.datetime(2026, 9, 18, 10, 30, 0)

PCT_DEFS = RISK_DEFS + [
    {"param_key": "max_total_invest_pct", "label": "총 투입 비중", "value_type": "decimal",
     "default_value": "100", "min_value": "0.1", "max_value": "100", "unit": "%"},
    {"param_key": "max_invest_per_stock_pct", "label": "종목당 최대 투입 비중",
     "value_type": "decimal", "default_value": "10", "min_value": "0.1", "max_value": "100",
     "unit": "%"},
]


def guard(**over) -> RiskGuard:
    return RiskGuard(meta={"code": "risk_guard"}, params=ParamSet(PCT_DEFS, over))


def ctx_with_asset(asset, *, age_sec: int = 0, db=None, **kw):
    balance = {"ord_alow_amt": 100_000_000, "entr": 100_000_000,
               "snapshot_at": NOW - _dt.timedelta(seconds=age_sec)}
    if asset is not None:
        balance["prsm_dpst_aset_amt"] = asset
    return make_ctx(db or FakeDb(), now=NOW, balance=balance, **kw)


def limit_buy(qty: int, price: int = 1) -> Signal:
    """한도 계산이 쉽도록 단가 1원짜리 지정가 신호(슬리피지 버퍼 미적용)."""
    return Signal(algo_code="momentum_screen", stk_cd="005930", side="BUY", qty=qty,
                  price=price, trde_tp="0", amount=qty * price, reason="테스트")


# ====================================================================== #
# effective_limit 순수 함수
# ====================================================================== #
@pytest.mark.parametrize("abs_won,pct,asset,expected", [
    (300_000, Decimal("10"), 1_000_000, 100_000),     # 비중이 더 작음
    (300_000, Decimal("10"), 10_000_000, 300_000),    # 절대가 더 작음
    (300_000, Decimal("30"), 1_000_000, 300_000),     # 정확히 같음 → 비중 쪽 선택
    (1_000_000, Decimal("100"), 187_200, 187_200),    # 현재 계좌 기준 총한도
    (300_000, Decimal("10"), 187_200, 18_720),        # 현재 계좌 기준 종목당 한도
])
def test_effective_limit_picks_min(abs_won, pct, asset, expected):
    limit, _ = effective_limit(abs_won, pct, asset)
    assert limit == expected


def test_effective_limit_describes_applied_side():
    _, desc = effective_limit(300_000, Decimal("10"), 1_000_000)
    assert desc == "비중 한도(10% = 100,000원)"
    _, desc2 = effective_limit(300_000, Decimal("10"), 10_000_000)
    assert desc2 == "절대 한도(300,000원)"


def test_effective_limit_trims_trailing_zeros_in_pct():
    _, desc = effective_limit(300_000, Decimal("12.50"), 1_000_000)
    assert "12.5%" in desc


# ====================================================================== #
# 비중 한도가 절대한도보다 작을 때 / 클 때
# ====================================================================== #
def test_pct_limit_blocks_when_tighter_than_absolute():
    """총자산 1,000,000 × 10% = 100,000 < 절대 300,000 → 비중 한도가 적용."""
    ctx = ctx_with_asset(1_000_000)
    ok, why = guard().check(ctx, limit_buy(100_001))
    assert ok is False
    assert "종목당 비중 한도(10% = 100,000원) 초과" in why


def test_within_pct_limit_passes():
    ctx = ctx_with_asset(1_000_000)
    ok, why = guard().check(ctx, limit_buy(100_000))
    assert ok is True, why


def test_absolute_limit_applies_when_tighter_than_pct():
    """총자산 10,000,000 × 10% = 1,000,000 > 절대 300,000 → 절대 한도가 적용."""
    ctx = ctx_with_asset(10_000_000)
    ok, why = guard().check(ctx, limit_buy(300_001))
    assert ok is False and "종목당 절대 한도(300,000원) 초과" in why


def test_total_pct_limit_blocks():
    """총 비중 5% → 1,000,000 × 5% = 50,000 이 총한도."""
    ctx = ctx_with_asset(1_000_000)
    ok, why = guard(max_total_invest_pct="5").check(ctx, limit_buy(50_001))
    assert ok is False and "총 비중 한도(5% = 50,000원) 초과" in why


def test_total_limit_counts_existing_positions():
    positions = {"000660": {"total_invested": 45_000}}
    ctx = ctx_with_asset(1_000_000, positions=positions)
    ok, why = guard(max_total_invest_pct="5").check(ctx, limit_buy(6_000))
    assert ok is False and "총 비중 한도" in why


# ====================================================================== #
# 경계값 (정확히 한도)
# ====================================================================== #
@pytest.mark.parametrize("amount,expected_ok", [
    (18_720, True),      # 187,200 × 10% 와 정확히 같음 → 통과
    (18_721, False),
])
def test_boundary_on_current_account_asset(amount, expected_ok):
    ctx = ctx_with_asset(187_200)
    ok, _ = guard().check(ctx, limit_buy(amount))
    assert ok is expected_ok


def test_pct_limit_rounds_down():
    """187,200 × 0.5% = 936.0 → 936원."""
    limit, _ = effective_limit(300_000, Decimal("0.5"), 187_200)
    assert limit == 936


# ====================================================================== #
# 총자산 미확인 → BUY 차단(fail-closed) / SELL 통과
# ====================================================================== #
@pytest.mark.parametrize("asset,age,fragment", [
    (0, 0, "추정예탁자산 0"),
    (None, 0, "추정예탁자산 0"),
    (1_000_000, BALANCE_STALE_SEC + 1, "초 경과"),
])
def test_unknown_asset_blocks_buy(asset, age, fragment):
    ctx = ctx_with_asset(asset, age_sec=age)
    ok, why = guard().check(ctx, limit_buy(1_000))
    assert ok is False
    assert "비중 한도 계산 불가로 차단" in why and fragment in why
    assert ctx.risk_halt                      # 사이클 전체 fail-closed


def test_missing_balance_blocks_buy():
    ctx = make_ctx(FakeDb(), now=NOW, balance={})
    ok, why = guard().check(ctx, limit_buy(1_000))
    assert ok is False and "비중 한도 계산 불가로 차단" in why


def test_sell_is_not_affected_by_asset_unknown():
    holdings = {"005930": {"stk_cd": "005930", "rmnd_qty": 10, "trde_able_qty": 10}}
    ctx = ctx_with_asset(0, holdings=holdings)
    sig = Signal(algo_code="risk_guard", stk_cd="005930", side="SELL", qty=10)
    ok, why = guard().check(ctx, sig)
    assert ok is True, why
    assert not ctx.risk_halt


def test_stop_loss_signal_still_generated_without_asset():
    holdings = {"005930": {"stk_cd": "005930", "stk_nm": "삼성전자", "rmnd_qty": 10,
                           "trde_able_qty": 10, "prft_rt": -20}}
    ctx = ctx_with_asset(0, holdings=holdings)
    assert len(guard().evaluate(ctx)) == 1


# ====================================================================== #
# 사이클 누적 + 슬리피지 버퍼
# ====================================================================== #
def test_cycle_invested_counts_toward_pct_limit():
    ctx = ctx_with_asset(1_000_000)
    ctx.record_cycle_invest("005930", 95_000)
    ok, why = guard().check(ctx, limit_buy(6_000))
    assert ok is False and "종목당 비중 한도" in why


def test_cycle_invested_counts_toward_total_pct_limit():
    ctx = ctx_with_asset(1_000_000)
    ctx.record_cycle_invest("000660", 48_000)
    ok, why = guard(max_total_invest_pct="5").check(ctx, limit_buy(3_000))
    assert ok is False and "총 비중 한도" in why


def test_slippage_buffer_applies_to_pct_limit():
    """시장가: 95,000 × 1.1 = 104,500 > 100,000(10%) → 차단."""
    ctx = ctx_with_asset(1_000_000)
    sig = limit_buy(95_000)
    sig.trde_tp = "3"
    ok, why = guard().check(ctx, sig)
    assert ok is False and "종목당 비중 한도" in why


def test_limit_order_has_no_buffer_on_pct_limit():
    ctx = ctx_with_asset(1_000_000)
    sig = limit_buy(95_000)
    sig.trde_tp = "0"
    assert guard().check(ctx, sig)[0] is True


# ====================================================================== #
# min/max 검증 (0 또는 100 초과 거부)
# ====================================================================== #
PDEF = {"param_key": "max_invest_per_stock_pct", "label": "종목당 최대 투입 비중",
        "value_type": "decimal", "default_value": "10", "min_value": "0.1", "max_value": "100"}


@pytest.mark.parametrize("value,ok", [
    ("0.1", True), ("10", True), ("100", True),
    ("0", False), ("100.1", False), ("-5", False), ("nan", False),
])
def test_pct_param_range(value, ok):
    if ok:
        assert validate(PDEF, value) == value
    else:
        with pytest.raises(ParamError):
            validate(PDEF, value)


def test_out_of_range_pct_disables_algorithm():
    from stock_svr.algo.registry import build

    meta = {"code": "risk_guard", "name": "가드", "role": "risk",
            "param_defs": [PDEF], "params": {"max_invest_per_stock_pct": "0"}}
    assert build(meta) is None


def test_default_pct_is_ten_percent():
    assert guard().params.dec("max_invest_per_stock_pct") == Decimal("10")
    assert guard().params.dec("max_total_invest_pct") == Decimal("100")


# ====================================================================== #
# 기존 절대 한도 동작 회귀
# ====================================================================== #
def test_zero_absolute_limit_still_blocks_before_pct_check():
    ctx = ctx_with_asset(10_000_000)
    ok, why = guard(max_invest_per_stock="0").check(ctx, buy_signal())
    assert ok is False and "종목당 최대 투입금이 0" in why
