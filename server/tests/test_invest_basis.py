"""투입 한도의 '기존 투입액' 기준 회귀 테스트 (2026-10).

예전엔 EngineContext.total_invested()/invested_in() 이 position_state.total_invested(이
프로그램이 스스로 누적한 값)만 합산해, 추적하지 못한 보유분(수동 매수 등)이 0원으로 빠지고
한도가 실제보다 느슨해졌다. 이제는 실제 보유현황(holding.pur_amt) 기준이며,
- 거래불가(상장폐지·정리매매 등, R-06) 종목은 제외하고
- 전송됐지만 아직 잔고에 반영되지 않은 매수(position_state 에만 있는 값)는 놓치지 않도록
  종목별로 max(pur_amt, position_state.total_invested) 를 쓰며
- 이번 사이클 승인분(cycle_invested*)은 예전처럼 그 위에 더한다.
"""
from __future__ import annotations

from conftest import FakeDb, make_ctx
from stock_svr.algo.params import ParamSet
from stock_svr.algo.risk_guard import RiskGuard
from stock_svr.engine.context import reset_untradable_log_state
from tests_support import RISK_DEFS, buy_signal


def guard(**over) -> RiskGuard:
    return RiskGuard(meta={"code": "risk_guard"}, params=ParamSet(RISK_DEFS, over))


def hold(stk_cd, pur_amt, *, stk_nm="정상종목", cur_prc=10_000, qty=1):
    return {"stk_cd": stk_cd, "stk_nm": stk_nm, "rmnd_qty": qty, "trde_able_qty": qty,
            "cur_prc": cur_prc, "pur_amt": pur_amt}


def setup_function(_):
    reset_untradable_log_state()


# ====================================================================== #
# 1. position_state 가 아예 없는 보유종목도 실제 매입금액으로 집계
# ====================================================================== #
def test_untracked_holding_is_counted():
    """핵심 버그: position_state 행이 없는 보유종목(수동 매수 등)이 0원으로 빠지면 안 된다."""
    holdings = {"181710": hold("181710", 58_400, stk_nm="NHN", cur_prc=56_400)}
    ctx = make_ctx(FakeDb(), holdings=holdings, positions={})
    assert ctx.total_invested() == 58_400
    assert ctx.invested_in("181710") == 58_400


def test_holding_amount_wins_over_smaller_tracked_amount():
    """position_state 가 실제보다 작게 드리프트한 경우 실제 매입금액(pur_amt)을 쓴다."""
    holdings = {"452190": hold("452190", 71_680, stk_nm="한빛레이저", cur_prc=6_000)}
    positions = {"452190": {"total_invested": 67_490}}
    ctx = make_ctx(FakeDb(), holdings=holdings, positions=positions)
    assert ctx.invested_in("452190") == 71_680
    assert ctx.total_invested() == 71_680


def test_tracked_amount_wins_when_larger_sent_but_unsynced():
    """전송 직후(체결·잔고 동기화 전) 물타기분은 position_state 에만 있다 - 큰 쪽을 쓴다."""
    holdings = {"005930": hold("005930", 100_000)}
    positions = {"005930": {"total_invested": 150_000}}
    ctx = make_ctx(FakeDb(), holdings=holdings, positions=positions)
    assert ctx.invested_in("005930") == 150_000
    assert ctx.total_invested() == 150_000


def test_unheld_tracked_buy_counted_until_sync():
    """신규 매수 전송 후 아직 보유목록에 없는 종목도 한도에서 빠지지 않는다."""
    positions = {"000660": {"total_invested": 80_000}}
    ctx = make_ctx(FakeDb(), holdings={}, positions=positions)
    assert ctx.total_invested() == 80_000
    assert ctx.invested_in("000660") == 80_000


def test_no_double_count_same_stock():
    """보유현황과 position_state 에 모두 있는 종목은 한 번만 센다."""
    holdings = {"005930": hold("005930", 100_000), "000660": hold("000660", 50_000)}
    positions = {"005930": {"total_invested": 100_000}, "000660": {"total_invested": 40_000}}
    ctx = make_ctx(FakeDb(), holdings=holdings, positions=positions)
    assert ctx.total_invested() == 150_000


def test_bad_pur_amt_values_are_ignored():
    holdings = {"A": hold("A", None), "B": hold("B", "abc"), "C": hold("C", 30_000)}
    ctx = make_ctx(FakeDb(), holdings=holdings)
    assert ctx.total_invested() == 30_000
    assert ctx.invested_in("A") == 0 and ctx.invested_in("B") == 0


# ====================================================================== #
# 2. 거래불가(상장폐지 등) 종목은 의도적으로 제외
# ====================================================================== #
def test_delisted_prefix_holding_excluded():
    """'(폐)' 표기 + 현재가 0 - 실제 계좌의 와이즈파워 사례."""
    holdings = {"040670": hold("040670", 361_000, stk_nm="(폐)와이즈파워", cur_prc=0, qty=200),
                "181710": hold("181710", 58_400, stk_nm="NHN", cur_prc=56_400)}
    ctx = make_ctx(FakeDb(), holdings=holdings)
    assert ctx.invested_in("040670") == 0
    assert ctx.total_invested() == 58_400


def test_zero_price_holding_excluded():
    holdings = {"111111": hold("111111", 200_000, stk_nm="이름정상", cur_prc=0)}
    ctx = make_ctx(FakeDb(), holdings=holdings)
    assert ctx.total_invested() == 0 and ctx.invested_in("111111") == 0


def test_stock_master_state_untradable_excluded():
    db = FakeDb()
    db.stock_states["222222"] = "정리매매"
    holdings = {"222222": hold("222222", 120_000, stk_nm="정리종목", cur_prc=500)}
    ctx = make_ctx(db, holdings=holdings)
    assert ctx.total_invested() == 0 and ctx.invested_in("222222") == 0


def test_untradable_excluded_even_with_tracked_amount():
    """거래불가 종목은 position_state 에 값이 있어도 제외한다."""
    holdings = {"040670": hold("040670", 361_000, stk_nm="(폐)와이즈파워", cur_prc=0)}
    positions = {"040670": {"total_invested": 361_000}}
    ctx = make_ctx(FakeDb(), holdings=holdings, positions=positions)
    assert ctx.total_invested() == 0 and ctx.invested_in("040670") == 0


def test_unheld_tracked_untradable_excluded_without_poisoning_cache():
    """미보유 + 거래불가(stock_master) 종목은 제외하되, 종목명 모르는 판정을 캐시에 남기지 않는다."""
    db = FakeDb()
    db.stock_states["333333"] = "거래정지"
    ctx = make_ctx(db, positions={"333333": {"total_invested": 50_000},
                                  "444444": {"total_invested": 10_000}})
    assert ctx.total_invested() == 10_000
    assert "444444" not in ctx._untradable
    # 나중에 '(폐)' 종목명으로 들어온 신호는 여전히 거래불가로 판정된다
    assert ctx.untradable_reason("444444", "(폐)어떤종목")


# ====================================================================== #
# 3. 이번 사이클 승인분은 예전처럼 위에 더해진다
# ====================================================================== #
def test_cycle_invested_adds_on_top_of_holdings():
    holdings = {"005930": hold("005930", 100_000), "000660": hold("000660", 50_000)}
    ctx = make_ctx(FakeDb(), holdings=holdings)
    ctx.record_cycle_invest("005930", 20_000)
    ctx.record_cycle_invest("035720", 30_000)
    assert ctx.total_invested() == 100_000 + 50_000 + 20_000 + 30_000
    assert ctx.invested_in("005930") == 120_000
    assert ctx.invested_in("035720") == 30_000
    assert ctx.invested_in("000660") == 50_000


def test_cycle_invested_on_untradable_stock_still_counts():
    """거래불가 제외는 '기존 보유분'에만 적용 - 사이클 승인분은 그대로 더한다."""
    holdings = {"040670": hold("040670", 361_000, stk_nm="(폐)와이즈파워", cur_prc=0)}
    ctx = make_ctx(FakeDb(), holdings=holdings)
    ctx.record_cycle_invest("040670", 5_000)
    assert ctx.invested_in("040670") == 5_000
    assert ctx.total_invested() == 5_000


# ====================================================================== #
# 4. risk_guard.check() 를 통한 종단 검증
# ====================================================================== #
def test_check_blocks_when_untracked_holdings_push_over_total_limit():
    """예전 계산(position_state 만)으로는 통과했을 매수가 실제 보유분 기준으로 차단된다.

    총한도 1,000,000. position_state 합계는 100,000 뿐이지만 실제 보유(추적 안 된 수동 매수
    포함)는 900,000 → 신규 매수 합계가 정확히 1,000,000 이면 통과, 1원이라도 넘으면 차단.
    """
    holdings = {"005930": hold("005930", 100_000),
                "181710": hold("181710", 400_000, stk_nm="NHN", cur_prc=56_400),
                "035720": hold("035720", 400_000, stk_nm="카카오", cur_prc=40_000)}
    positions = {"005930": {"total_invested": 100_000}}      # 수동 매수 2종목은 추적 안 됨
    ctx = make_ctx(FakeDb(), holdings=holdings, positions=positions)

    # 예전 기준이었다면 100,000 + 100,001 <= 1,000,000 으로 통과했을 신호
    ok, why = guard().check(ctx, buy_signal(stk="000660", qty=1, price=100_001))
    assert ok is False
    assert "총 절대 한도" in why and "기존 900,000" in why

    ok2, why2 = guard().check(ctx, buy_signal(stk="000660", qty=1, price=100_000))
    assert ok2 is True, why2


def test_check_per_stock_uses_untracked_holding():
    """추적 안 된 보유종목에 추가 매수 시 종목당 한도가 실제 매입금액 기준으로 걸린다."""
    holdings = {"181710": hold("181710", 250_000, stk_nm="NHN", cur_prc=56_400)}
    ctx = make_ctx(FakeDb(), holdings=holdings, positions={})
    ok, why = guard().check(ctx, buy_signal(stk="181710", qty=1, price=50_001))
    assert ok is False and "종목당 절대 한도" in why and "기존 250,000" in why
    ok2, why2 = guard().check(ctx, buy_signal(stk="181710", qty=1, price=50_000))
    assert ok2 is True, why2


def test_check_delisted_holding_does_not_consume_total_limit():
    """상장폐지 종목의 매입금액은 총한도를 차지하지 않는다(의도된 제외)."""
    holdings = {"040670": hold("040670", 900_000, stk_nm="(폐)와이즈파워", cur_prc=0, qty=200)}
    ctx = make_ctx(FakeDb(), holdings=holdings)
    ok, why = guard().check(ctx, buy_signal(stk="005930", qty=10, price=20_000))   # 200,000
    assert ok is True, why


def test_live_account_snapshot_arithmetic():
    """2026-10-07 실계좌 보유현황 스냅샷으로 새 기준 합계를 고정한다.

    예전(position_state 합계) 443,470원 → 새 기준 506,320원
    (NHN 58,400 포함, (폐)와이즈파워 361,000 제외, 종목별 max(pur_amt, position_state)).
    """
    holdings = {
        "006730": hold("006730", 91_600, stk_nm="서부T&D", cur_prc=12_000),
        "040670": hold("040670", 361_000, stk_nm="(폐)와이즈파워", cur_prc=0, qty=200),
        "051370": hold("051370", 96_070, stk_nm="인터플렉스", cur_prc=7_130),
        "083450": hold("083450", 52_700, stk_nm="GST", cur_prc=55_300),
        "181710": hold("181710", 58_400, stk_nm="NHN", cur_prc=56_400),
        "452190": hold("452190", 71_680, stk_nm="한빛레이저", cur_prc=6_000),
        "457370": hold("457370", 34_470, stk_nm="한켐", cur_prc=10_830),
        "484590": hold("484590", 98_980, stk_nm="삼양컴텍", cur_prc=7_120),
    }
    positions = {
        "006730": {"total_invested": 91_440}, "051370": {"total_invested": 96_070},
        "083450": {"total_invested": 52_600}, "452190": {"total_invested": 67_490},
        "457370": {"total_invested": 36_470}, "484590": {"total_invested": 99_400},
    }
    ctx = make_ctx(FakeDb(), holdings=holdings, positions=positions)
    assert sum(p["total_invested"] for p in positions.values()) == 443_470   # 예전 값
    assert ctx.total_invested() == 506_320
