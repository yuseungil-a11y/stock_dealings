"""universe_filter — 시총 순위·제외 규칙·주가 필터·fail-closed·UI 미리보기 테스트.

fake DB 만 사용하며 실 DB·실 API 를 호출하지 않는다.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from conftest import FakeDb, make_ctx
from stock_svr.algo.base import (
    KIND_AVG_DOWN,
    KIND_ENTRY,
    KIND_LIQUIDATE,
    KIND_STOP_LOSS,
    Signal,
)
from stock_svr.algo.params import ParamSet
from stock_svr.algo.risk_guard import limit_preview
from stock_svr.algo.universe_filter import (
    EOK,
    UniverseFilter,
    UniverseOptions,
    build_universe,
    clear_universe_cache,
    is_preferred_name,
    load_universe,
    master_age_days,
)

NOW = _dt.datetime(2026, 9, 18, 10, 30)     # 금요일 장중

# ---------------------------------------------------------------------- #
# seed.sql 의 algorithm_param_def 와 같은 정의
# ---------------------------------------------------------------------- #
DEFS = [
    {"param_key": "use_kospi", "label": "코스피 포함", "value_type": "bool", "default_value": "1"},
    {"param_key": "use_kosdaq", "label": "코스닥 포함", "value_type": "bool", "default_value": "1"},
    {"param_key": "use_etf", "label": "ETF 포함", "value_type": "bool", "default_value": "1"},
    {"param_key": "rank_scope", "label": "순위 기준", "value_type": "enum",
     "default_value": "per_market",
     "enum_options": "per_market:시장별 순위,combined:선택된 시장 합산 순위"},
    {"param_key": "top_n", "label": "시가총액 상위 N", "value_type": "int",
     "default_value": "100", "min_value": "1", "max_value": "2000"},
    {"param_key": "min_market_cap_eok", "label": "최소 시가총액", "value_type": "int",
     "default_value": "0", "min_value": "0", "max_value": "100000000"},
    {"param_key": "min_price", "label": "최소 주가(1주)", "value_type": "int",
     "default_value": "50000", "min_value": "0", "max_value": "10000000"},
    {"param_key": "max_price", "label": "최대 주가(1주)", "value_type": "int",
     "default_value": "0", "min_value": "0", "max_value": "100000000"},
    {"param_key": "exclude_preferred", "label": "우선주 제외", "value_type": "bool",
     "default_value": "1"},
    {"param_key": "exclude_spac", "label": "스팩 제외", "value_type": "bool", "default_value": "1"},
    {"param_key": "exclude_warning", "label": "관리·경고 종목 제외", "value_type": "bool",
     "default_value": "1"},
    {"param_key": "apply_to", "label": "적용 대상", "value_type": "enum", "default_value": "entry",
     "enum_options": "entry:신규 진입 매수만,entry_and_avg:신규 진입 + 물타기"},
    {"param_key": "stale_days", "label": "종목마스터 허용 경과일", "value_type": "int",
     "default_value": "5", "min_value": "1", "max_value": "30"},
]


def opts(**over) -> UniverseOptions:
    return UniverseOptions.from_params(ParamSet(DEFS, {k: str(v) for k, v in over.items()}))


def algo(**over) -> UniverseFilter:
    return UniverseFilter(meta={"code": "universe_filter", "name": "유니버스"},
                          params=ParamSet(DEFS, {k: str(v) for k, v in over.items()}))


MARKET_NAME = {"0": "거래소", "10": "코스닥", "8": "ETF", "60": "ETN"}


def master(stk_cd, stk_nm, market_code="0", list_count=10_000_000, last_price=100_000,
           state=None, order_warning="0") -> dict:
    return {"stk_cd": stk_cd, "stk_nm": stk_nm, "market_code": market_code,
            "market_name": MARKET_NAME.get(market_code, market_code),
            "list_count": list_count, "last_price": last_price,
            "state": state, "order_warning": order_warning}


def rows_basic() -> list[dict]:
    """코스피 3 + 코스닥 3 (시총 내림차순으로 읽기 쉽게)."""
    return [
        master("005930", "삼성전자", "0", 1_000_000, 70_000),       # 700억
        master("000660", "SK하이닉스", "0", 500_000, 100_000),      # 500억
        master("051910", "LG화학", "0", 100_000, 300_000),          # 300억
        master("247540", "에코프로비엠", "10", 900_000, 200_000),   # 1,800억
        master("091990", "셀트리온헬스케어", "10", 400_000, 100_000),  # 400억
        master("035760", "CJ ENM", "10", 100_000, 60_000),          # 60억
    ]


def rows_etf() -> list[dict]:
    """ETF 3종목 (시총 내림차순). 이름은 실제 ETF 명명 규칙을 따른다."""
    return [
        master("069500", "KODEX 200", "8", 1_000_000, 40_000),        # 400억
        master("360750", "TIGER 미국S&P500", "8", 1_000_000, 20_000),  # 200억
        master("122630", "KODEX 레버리지", "8", 100_000, 15_000),      # 15억
    ]


def buy(stk="005930", kind=KIND_ENTRY, price=None, meta=None) -> Signal:
    return Signal(algo_code="momentum_screen", stk_cd=stk, stk_nm="테스트", side="BUY",
                  qty=1, price=price, kind=kind, meta=meta or {})


def db_with(rows, updated_at=NOW) -> FakeDb:
    db = FakeDb()
    db.stock_master_rows = list(rows)
    db.stock_master_updated_at = updated_at
    return db


# ====================================================================== #
# 1. 순위 계산
# ====================================================================== #
def test_rank_per_market():
    uni = build_universe(rows_basic(), opts(min_price=0))
    assert uni.entries["005930"].rank == 1       # 코스피 1위
    assert uni.entries["000660"].rank == 2
    assert uni.entries["051910"].rank == 3
    assert uni.entries["247540"].rank == 1       # 코스닥 1위
    assert uni.entries["091990"].rank == 2


def test_rank_combined():
    uni = build_universe(rows_basic(), opts(rank_scope="combined", min_price=0))
    # 시총: 에코프로비엠 1800 > 삼성전자 700 > SK하이닉스 500 > 셀트리온 400 > LG화학 300 > CJ 60
    assert [e.stk_cd for e in uni.ranked] == [
        "247540", "005930", "000660", "091990", "051910", "035760"]
    assert uni.entries["247540"].rank == 1 and uni.entries["005930"].rank == 2


def test_rank_tie_is_deterministic_by_code():
    rows = [master("000002", "B사", "0", 100, 1000), master("000001", "A사", "0", 100, 1000)]
    uni = build_universe(rows, opts(min_price=0, rank_scope="combined"))
    assert [e.stk_cd for e in uni.ranked] == ["000001", "000002"]


def test_only_selected_markets_are_ranked():
    uni = build_universe(rows_basic(), opts(use_kosdaq=0, min_price=0))
    assert {e.market_code for e in uni.ranked} == {"0"}
    assert "대상 시장 아님" in uni.entries["247540"].excluded


def test_non_target_market_rows_are_ignored():
    """ETN·금현물 등 코스피/코스닥/ETF 가 아닌 시장은 유니버스에 들어오지 않는다."""
    rows = rows_basic() + [master("580011", "ETN상품", "60", 1_000_000, 40_000),
                           master("411060", "금현물", "6", 1_000_000, 40_000)]
    uni = build_universe(rows, opts(min_price=0))
    assert "580011" not in uni.entries and "411060" not in uni.entries


# ====================================================================== #
# 1-1. ETF (use_etf)
# ====================================================================== #
def test_etf_is_included_by_default():
    uni = build_universe(rows_basic() + rows_etf(), opts(min_price=0))
    etf = uni.entries["069500"]
    assert etf.excluded == "" and etf.market_label == "ETF"
    assert etf.passed is True


def test_etf_excluded_when_use_etf_off():
    uni = build_universe(rows_basic() + rows_etf(), opts(min_price=0, use_etf=0))
    assert "대상 시장 아님(ETF)" in uni.entries["069500"].excluded
    assert "069500" not in [e.stk_cd for e in uni.ranked]


def test_etf_buy_blocked_when_use_etf_off():
    ctx = make_ctx(db_with(rows_basic() + rows_etf()), now=NOW)
    kept = algo(min_price=0, use_etf=0).filter_signals(ctx, [buy("069500")])
    assert kept == [] and "대상 시장 아님(ETF)" in ctx.db.signals[-1]["detail"]


def test_etf_buy_passes_when_use_etf_on():
    ctx = make_ctx(db_with(rows_basic() + rows_etf()), now=NOW)
    sig = buy("069500")
    assert algo(min_price=0).filter_signals(ctx, [sig]) == [sig]
    assert ctx.db.signals == []


def test_etf_has_own_rank_in_per_market():
    """per_market 이면 ETF 도 자기 그룹 안에서 1위부터 순위를 매긴다."""
    uni = build_universe(rows_basic() + rows_etf(), opts(min_price=0))
    assert uni.entries["069500"].rank == 1       # ETF 1위
    assert uni.entries["360750"].rank == 2
    assert uni.entries["122630"].rank == 3
    assert uni.entries["005930"].rank == 1       # 코스피 1위는 그대로
    assert uni.entries["247540"].rank == 1       # 코스닥 1위도 그대로


def test_etf_top_n_is_counted_per_market():
    """top_n=2 면 코스피 2 + 코스닥 2 + ETF 2 가 각각 통과한다."""
    o = opts(min_price=0, top_n=2)
    uni = build_universe(rows_basic() + rows_etf(), o)
    assert uni.passed_by_market == {"0": 2, "10": 2, "8": 2}
    assert uni.passed_total == 6


def test_etf_over_top_n_message_uses_etf_label():
    ctx = make_ctx(db_with(rows_basic() + rows_etf()), now=NOW)
    kept = algo(min_price=0, top_n=2).filter_signals(ctx, [buy("122630")])
    assert kept == []
    assert "ETF 시총순위 3위 > 2" in ctx.db.signals[-1]["detail"]


def test_etf_joins_combined_ranking():
    """combined 면 ETF 도 코스피·코스닥과 하나의 시총 순위에 들어간다."""
    uni = build_universe(rows_basic() + rows_etf(), opts(min_price=0, rank_scope="combined"))
    # 1800(에코프로비엠) > 700(삼성) > 500(하이닉스) > 400(셀트리온·KODEX200) > 300 > 200 > 60 > 15
    assert [e.stk_cd for e in uni.ranked] == [
        "247540", "005930", "000660", "069500", "091990", "051910", "360750",
        "035760", "122630"]
    assert uni.entries["069500"].rank == 4


def test_combined_label_includes_etf():
    ctx = make_ctx(db_with(rows_basic() + rows_etf()), now=NOW)
    algo(min_price=0, top_n=1, rank_scope="combined").filter_signals(ctx, [buy("069500")])
    assert "코스피+코스닥+ETF 시총순위 4위 > 1" in ctx.db.signals[-1]["detail"]


def test_etf_only_universe():
    o = opts(min_price=0, use_kospi=0, use_kosdaq=0)
    uni = build_universe(rows_basic() + rows_etf(), o)
    assert {e.market_code for e in uni.ranked} == {"8"}
    assert o.markets_text == "ETF"


@pytest.mark.parametrize("price,passes", [(19_999, False), (20_000, True)])
def test_min_price_applies_to_etf(price, passes):
    rows = [master("360750", "TIGER 미국S&P500", "8", 1_000_000, price)]
    ctx = make_ctx(db_with(rows), now=NOW)
    assert bool(algo(min_price=20_000).filter_signals(ctx, [buy("360750")])) is passes


def test_max_price_applies_to_etf():
    rows = [master("069500", "KODEX 200", "8", 1_000_000, 40_000)]
    ctx = make_ctx(db_with(rows), now=NOW)
    kept = algo(min_price=0, max_price=30_000).filter_signals(ctx, [buy("069500")])
    assert kept == [] and "주가 40,000원 > 최대 30,000원" in ctx.db.signals[-1]["detail"]


def test_exclude_warning_applies_to_etf():
    rows = rows_etf() + [master("000000", "정리중ETF", "8", 1_000_000, 30_000,
                                state="증거금100%|거래정지")]
    uni = build_universe(rows, opts(min_price=0))
    assert uni.entries["000000"].excluded == "거래정지"


def test_order_warning_applies_to_etf():
    rows = [master("069500", "KODEX 200", "8", 1_000_000, 40_000, order_warning="3")]
    uni = build_universe(rows, opts(min_price=0))
    assert "투자유의" in uni.entries["069500"].excluded


def test_etf_market_cap_is_list_count_times_last_price():
    uni = build_universe(rows_etf(), opts(min_price=0))
    assert uni.entries["069500"].market_cap == 1_000_000 * 40_000
    assert uni.entries["069500"].cap_eok == 400


def test_min_market_cap_applies_to_etf():
    ctx = make_ctx(db_with(rows_etf()), now=NOW)
    kept = algo(min_price=0, min_market_cap_eok=100).filter_signals(ctx, [buy("122630")])
    assert kept == [] and "시가총액 15억원 < 최소 100억원" in ctx.db.signals[-1]["detail"]


@pytest.mark.parametrize("name", [
    "KODEX 200", "TIGER 미국S&P500", "KODEX 레버리지", "RISE 200", "ACE 글로벌반도체TOP4 Plus",
    "TIGER 차이나전기차SOLACTIVE", "KODEX 은행", "PLUS 고배당주",
])
def test_normal_etf_names_are_not_excluded_by_name_rules(name):
    """우선주·스팩 제외가 켜져 있어도 정상적인 ETF 이름은 걸리지 않는다(오탐 없음)."""
    rows = rows_basic() + [master("069500", name, "8", 1_000_000, 40_000)]
    uni = build_universe(rows, opts(min_price=0, exclude_preferred=1, exclude_spac=1))
    assert uni.entries["069500"].excluded == ""


def test_etf_missing_price_is_excluded():
    rows = [master("069500", "KODEX 200", "8", 1_000_000, 0)]
    uni = build_universe(rows, opts(min_price=0))
    assert "시가총액 계산 불가" in uni.entries["069500"].excluded


# ====================================================================== #
# 2. 제외 규칙
# ====================================================================== #
@pytest.mark.parametrize("name,expected", [
    ("삼성전자우", True), ("현대차2우B", True), ("삼성전자우(전환)", True),
    ("우리금융지주", False), ("우진", False), ("미래에셋대우", False),
    ("삼성전자", False), ("우", False),
])
def test_preferred_name_detection(name, expected):
    base = {"삼성전자", "현대차", "우리금융지주", "우진", "미래에셋대우"}
    assert is_preferred_name(name, base) is expected


@pytest.mark.parametrize("name,code,expected", [
    ("삼성전자우", "005935", True),        # ① 보통주 이름 존재
    ("남선알미우", "008355", True),        # ② 끝자리 0 인 보통주 코드 존재(이름 축약 우선주)
    ("코리아써우", "007815", True),
    ("에코글로우", "159910", False),       # 끝자리 0 = 보통주 자신
    ("이오플로우", "294090", False),
    ("성우", "458650", False),
    ("우리금융지주", "316140", False),
    ("미래에셋대우", "006805", False),     # 보통주 이름도, 끝자리 0 코드도 없음
])
def test_is_preferred_with_code_rule(name, code, expected):
    from stock_svr.algo.universe_filter import is_preferred

    names = {"삼성전자", "남선알미늄", "코리아써키트", "에코글로우", "이오플로우", "성우",
             "우리금융지주", name}
    codes = {"005930", "008350", "007810", "159910", "294090", "458650", "316140", code}
    assert is_preferred(name, code, names, codes) is expected


def test_exclude_preferred_stock():
    rows = rows_basic() + [master("005935", "삼성전자우", "0", 100_000, 60_000)]
    uni = build_universe(rows, opts(min_price=0))
    assert uni.entries["005935"].excluded == "우선주"
    assert "005935" not in [e.stk_cd for e in uni.ranked]


def test_keep_preferred_when_option_off():
    rows = rows_basic() + [master("005935", "삼성전자우", "0", 100_000, 60_000)]
    uni = build_universe(rows, opts(min_price=0, exclude_preferred=0))
    assert uni.entries["005935"].excluded == ""


def test_exclude_spac():
    rows = rows_basic() + [master("123456", "NH스팩29호", "10", 10_000_000, 2_000)]
    uni = build_universe(rows, opts(min_price=0))
    assert uni.entries["123456"].excluded == "스팩"


def test_exclude_warning_by_state():
    rows = [master("000001", "관리중", "0", 100_000, 100_000, state="증거금100%|거래정지")]
    uni = build_universe(rows, opts(min_price=0))
    assert uni.entries["000001"].excluded == "거래정지"


def test_exclude_warning_by_order_warning():
    rows = [master("000001", "유의종목", "0", 100_000, 100_000, order_warning="2")]
    uni = build_universe(rows, opts(min_price=0))
    assert "투자유의" in uni.entries["000001"].excluded


def test_normal_state_is_not_excluded():
    rows = [master("000001", "정상", "0", 100_000, 100_000,
                   state="증거금40%|담보대출|신용가능", order_warning="0")]
    uni = build_universe(rows, opts(min_price=0))
    assert uni.entries["000001"].excluded == ""


def test_missing_list_count_is_excluded():
    rows = [master("000001", "정보없음", "0", None, 100_000)]
    uni = build_universe(rows, opts(min_price=0))
    assert "시가총액 계산 불가" in uni.entries["000001"].excluded


# ====================================================================== #
# 3. 통과 조건 (순위 / 시총 / 주가)
# ====================================================================== #
def test_top_n_blocks_lower_rank():
    ctx = make_ctx(db_with(rows_basic()), now=NOW)
    kept = algo(top_n=1, min_price=0).filter_signals(ctx, [buy("000660")])
    assert kept == []
    detail = ctx.db.signals[-1]["detail"]
    assert "코스피 시총순위 2위 > 1" in detail


def test_top_n_message_uses_combined_label():
    ctx = make_ctx(db_with(rows_basic()), now=NOW)
    algo(top_n=1, min_price=0, use_etf=0,
         rank_scope="combined").filter_signals(ctx, [buy("005930")])
    assert "코스피+코스닥 시총순위 2위 > 1" in ctx.db.signals[-1]["detail"]


def test_min_market_cap_blocks():
    ctx = make_ctx(db_with(rows_basic()), now=NOW)
    # CJ ENM 은 60억 < 100억
    kept = algo(min_price=0, min_market_cap_eok=100).filter_signals(ctx, [buy("035760")])
    assert kept == []
    assert "시가총액 60억원 < 최소 100억원" in ctx.db.signals[-1]["detail"]


@pytest.mark.parametrize("price,passes", [(49_999, False), (50_000, True), (50_001, True)])
def test_min_price_boundary(price, passes):
    rows = [master("000001", "경계", "0", 1_000_000, price)]
    ctx = make_ctx(db_with(rows), now=NOW)
    kept = algo(min_price=50_000).filter_signals(ctx, [buy("000001")])
    assert bool(kept) is passes
    if not passes:
        assert f"주가 {price:,}원 < 최소 50,000원" in ctx.db.signals[-1]["detail"]


def test_max_price_blocks():
    rows = [master("000001", "고가", "0", 1_000_000, 900_000)]
    ctx = make_ctx(db_with(rows), now=NOW)
    kept = algo(min_price=50_000, max_price=500_000).filter_signals(ctx, [buy("000001")])
    assert kept == []
    assert "주가 900,000원 > 최대 500,000원" in ctx.db.signals[-1]["detail"]


def test_zero_min_price_means_unused():
    rows = [master("000001", "동전주", "0", 1_000_000, 500)]
    ctx = make_ctx(db_with(rows), now=NOW)
    assert algo(min_price=0).filter_signals(ctx, [buy("000001")])


def test_signal_price_overrides_last_price():
    """신호 시점 현재가가 하한 미만이면 전일종가가 높아도 차단한다."""
    rows = [master("000001", "급락주", "0", 1_000_000, 60_000)]
    ctx = make_ctx(db_with(rows), now=NOW)
    kept = algo(min_price=50_000).filter_signals(ctx, [buy("000001", meta={"cur_prc": 32_000})])
    assert kept == []
    assert "주가 32,000원 < 최소 50,000원" in ctx.db.signals[-1]["detail"]


def test_falls_back_to_last_price_without_signal_price():
    rows = [master("000001", "시세없음", "0", 1_000_000, 40_000)]
    ctx = make_ctx(db_with(rows), now=NOW)
    kept = algo(min_price=50_000).filter_signals(ctx, [buy("000001")])
    assert kept == [] and "40,000원" in ctx.db.signals[-1]["detail"]


def test_unknown_stock_is_blocked():
    ctx = make_ctx(db_with(rows_basic()), now=NOW)
    kept = algo(min_price=0).filter_signals(ctx, [buy("999999")])
    assert kept == [] and "종목마스터에 없음" in ctx.db.signals[-1]["detail"]


def test_passing_signal_is_silent():
    ctx = make_ctx(db_with(rows_basic()), now=NOW)
    sig = buy("005930")
    assert algo(min_price=0).filter_signals(ctx, [sig]) == [sig]
    assert ctx.db.signals == []


# ====================================================================== #
# 4. 매도·손절·청산은 절대 건드리지 않는다
# ====================================================================== #
@pytest.mark.parametrize("kind", [KIND_STOP_LOSS, KIND_LIQUIDATE, KIND_ENTRY])
def test_sell_signals_never_blocked(kind):
    """마스터가 비어 fail-closed 인 상황에서도 SELL 은 그대로 통과한다."""
    db = db_with([], updated_at=None)
    ctx = make_ctx(db, now=NOW)
    sell = Signal(algo_code="risk_guard", stk_cd="005930", side="SELL", qty=10, kind=kind)
    assert algo().filter_signals(ctx, [sell]) == [sell]
    assert db.signals == []


def test_sell_of_out_of_universe_stock_passes():
    ctx = make_ctx(db_with(rows_basic()), now=NOW)
    sell = Signal(algo_code="risk_guard", stk_cd="035760", side="SELL", qty=10,
                  kind=KIND_STOP_LOSS)
    entry = buy("035760")
    kept = algo(top_n=1, min_price=0).filter_signals(ctx, [sell, entry])
    assert kept == [sell]


def test_no_db_access_when_only_sell_signals():
    db = db_with(rows_basic())
    db.fail_on.add("stock_master_stats")     # 조회하면 예외 → 조회하지 않음을 검증
    ctx = make_ctx(db, now=NOW)
    sell = Signal(algo_code="risk_guard", stk_cd="005930", side="SELL", qty=1,
                  kind=KIND_STOP_LOSS)
    assert algo().filter_signals(ctx, [sell]) == [sell]


# ====================================================================== #
# 5. apply_to 매트릭스
# ====================================================================== #
@pytest.mark.parametrize("apply_to,kind,blocked", [
    ("entry", KIND_ENTRY, True),
    ("entry", KIND_AVG_DOWN, False),
    ("entry_and_avg", KIND_ENTRY, True),
    ("entry_and_avg", KIND_AVG_DOWN, True),
])
def test_apply_to_matrix(apply_to, kind, blocked):
    ctx = make_ctx(db_with(rows_basic()), now=NOW)
    sig = buy("035760", kind=kind)            # 시총 최하위 → top_n=1 이면 차단 대상
    kept = algo(apply_to=apply_to, top_n=1, min_price=0).filter_signals(ctx, [sig])
    assert (kept == []) is blocked


# ====================================================================== #
# 6. fail-closed
# ====================================================================== #
def test_empty_master_blocks_buy():
    ctx = make_ctx(db_with([], updated_at=None), now=NOW)
    kept = algo().filter_signals(ctx, [buy()])
    assert kept == [] and "비어 있음" in ctx.db.signals[-1]["detail"]


def test_stale_master_blocks_buy():
    old = NOW - _dt.timedelta(days=9)
    ctx = make_ctx(db_with(rows_basic(), updated_at=old), now=NOW)
    kept = algo(stale_days=5, min_price=0).filter_signals(ctx, [buy()])
    assert kept == [] and "9일 전 갱신" in ctx.db.signals[-1]["detail"]


def test_fresh_master_within_stale_days_passes():
    ok = NOW - _dt.timedelta(days=5)
    ctx = make_ctx(db_with(rows_basic(), updated_at=ok), now=NOW)
    assert algo(stale_days=5, min_price=0).filter_signals(ctx, [buy()])


def test_query_failure_blocks_buy():
    db = db_with(rows_basic())
    db.fail_on.add("stock_master_universe")
    ctx = make_ctx(db, now=NOW)
    kept = algo(min_price=0).filter_signals(ctx, [buy()])
    assert kept == [] and "조회 실패" in db.signals[-1]["detail"]


def test_stats_failure_blocks_buy():
    db = db_with(rows_basic())
    db.fail_on.add("stock_master_stats")
    ctx = make_ctx(db, now=NOW)
    assert algo(min_price=0).filter_signals(ctx, [buy()]) == []


def test_master_age_days_none_when_unknown():
    assert master_age_days(None, NOW) is None


# ====================================================================== #
# 7. 캐시
# ====================================================================== #
class _CountingDb(FakeDb):
    def __init__(self):
        super().__init__()
        self.universe_calls = 0

    def stock_master_universe(self, market_codes=("0", "10")):
        self.universe_calls += 1
        return super().stock_master_universe(market_codes)


def _counting_db():
    db = _CountingDb()
    db.stock_master_rows = rows_basic()
    db.stock_master_updated_at = NOW
    return db


def test_ranking_is_cached_between_cycles():
    db = _counting_db()
    o = opts(min_price=0)
    for _ in range(3):
        uni, err = load_universe(db, o, NOW)
        assert uni is not None and not err
    assert db.universe_calls == 1


def test_cache_invalidated_when_master_updated():
    db = _counting_db()
    o = opts(min_price=0)
    load_universe(db, o, NOW)
    db.stock_master_updated_at = NOW + _dt.timedelta(hours=1)
    load_universe(db, o, NOW)
    assert db.universe_calls == 2


def test_cache_separated_by_options():
    db = _counting_db()
    load_universe(db, opts(min_price=0), NOW)
    load_universe(db, opts(min_price=0, top_n=5), NOW)
    assert db.universe_calls == 2


def test_cache_separated_by_use_etf():
    db = _counting_db()
    load_universe(db, opts(min_price=0), NOW)
    load_universe(db, opts(min_price=0, use_etf=0), NOW)
    assert db.universe_calls == 2


def test_clear_universe_cache_forces_reload():
    db = _counting_db()
    o = opts(min_price=0)
    load_universe(db, o, NOW)
    clear_universe_cache()
    load_universe(db, o, NOW)
    assert db.universe_calls == 2


def test_sync_stock_master_clears_cache():
    """종목마스터 갱신 후에는 순위를 다시 계산한다."""
    from stock_svr.services.sync_market import MarketService

    db = _counting_db()
    o = opts(min_price=0)
    load_universe(db, o, NOW)
    MarketService._invalidate_universe()
    load_universe(db, o, NOW)
    assert db.universe_calls == 2


# ====================================================================== #
# 8. 파라미터 검증 (정의만으로 표현할 수 없는 제약)
# ====================================================================== #
def test_max_price_below_min_price_is_error():
    errs = UniverseFilter.validate_params(
        ParamSet(DEFS, {"min_price": "50000", "max_price": "30000"}))
    assert errs and "최소 주가" in errs[0]


def test_max_price_zero_is_allowed():
    assert UniverseFilter.validate_params(
        ParamSet(DEFS, {"min_price": "50000", "max_price": "0"})) == []


def test_all_markets_off_is_error():
    errs = UniverseFilter.validate_params(
        ParamSet(DEFS, {"use_kospi": "0", "use_kosdaq": "0", "use_etf": "0"}))
    assert errs and "코스피" in errs[0] and "ETF" in errs[0]


@pytest.mark.parametrize("params", [
    {"use_kospi": "0", "use_kosdaq": "0"},            # ETF 만 켜짐
    {"use_kospi": "0", "use_etf": "0"},               # 코스닥만 켜짐
    {"use_kosdaq": "0", "use_etf": "0"},              # 코스피만 켜짐
])
def test_one_market_on_is_not_error(params):
    assert UniverseFilter.validate_params(ParamSet(DEFS, params)) == []


def test_registry_disables_algo_on_cross_param_error():
    from stock_svr.algo import registry

    seen: list[tuple] = []
    meta = {"code": "universe_filter", "name": "유니버스", "role": "filter", "is_locked": 0,
            "param_defs": DEFS,
            "params": {"use_kospi": "0", "use_kosdaq": "0", "use_etf": "0"}}
    assert registry.build(meta, on_error=lambda c, d, crit: seen.append((c, d, crit))) is None
    assert seen and "파라미터 오류" in seen[0][1]


def test_registry_builds_with_valid_params():
    from stock_svr.algo import registry

    meta = {"code": "universe_filter", "name": "유니버스", "role": "filter", "is_locked": 0,
            "param_defs": DEFS, "params": {}}
    assert isinstance(registry.build(meta), UniverseFilter)


def test_all_markets_off_blocks_buy():
    ctx = make_ctx(db_with(rows_basic()), now=NOW)
    kept = algo(use_kospi=0, use_kosdaq=0, use_etf=0).filter_signals(ctx, [buy()])
    assert kept == [] and "파라미터 오류" in ctx.db.signals[-1]["detail"]


# ====================================================================== #
# 9. 유효 한도 미리보기 (순수 계산)
# ====================================================================== #
def test_limit_preview_pct_is_binding():
    p = limit_preview(187_200, 1_000_000, 100, 300_000, 10, min_price=50_000)
    assert p.per_limit == 18_720 and "비중 한도" in p.per_desc
    assert "현재 종목당 한도 18,720원으로는 1주 50,000원 종목을 매수할 수 없습니다" in p.warning
    assert "종목당 비중을 26.71% 이상" in p.warning
    assert p.required_pct is not None and 187_200 * p.required_pct / 100 >= 50_000


def test_limit_preview_absolute_is_binding():
    p = limit_preview(10_000_000, 1_000_000, 100, 30_000, 10, min_price=50_000)
    assert p.per_limit == 30_000 and "절대 한도" in p.per_desc
    assert "절대 한도)을 50,000원 이상으로" in p.warning


def test_limit_preview_no_warning_when_enough():
    p = limit_preview(10_000_000, 1_000_000, 100, 300_000, 10, min_price=50_000)
    assert p.per_limit == 300_000 and p.warning == "" and p.has_warning is False


def test_limit_preview_without_asset():
    p = limit_preview(None, 1_000_000, 100, 300_000, 10, min_price=50_000)
    assert p.asset is None and p.asset_text == "확인 불가"
    assert p.per_limit == 300_000 and p.warning == ""


def test_limit_preview_without_asset_warns_on_small_absolute():
    p = limit_preview(0, 1_000_000, 100, 10_000, 10, min_price=50_000)
    assert "매수할 수 없습니다" in p.warning


def test_limit_preview_required_pct_over_100():
    p = limit_preview(30_000, 1_000_000, 100, 300_000, 10, min_price=50_000)
    assert p.required_pct > 100 and "예수금" in p.warning


def test_limit_preview_ignores_min_price_zero():
    p = limit_preview(10_000, 1_000_000, 100, 300_000, 10, min_price=0)
    assert p.warning == ""


# ====================================================================== #
# 10. UI — 미리보기 표 / 자동거래 확인창 요약
# ====================================================================== #
def test_preview_summary_and_rows():
    from stock_svr.ui.universe_preview import preview_rows, summary_lines

    o = opts(min_price=0, top_n=2)
    uni = build_universe(rows_basic(), o, updated_at=NOW)
    lines = "\n".join(summary_lines(uni, o))
    assert "총 4종목" in lines and "코스피 2종목" in lines and "코스닥 2종목" in lines
    assert "2026-09-18 10:30" in lines
    # 컷오프 = 마지막 통과 종목의 시총 (코스피 2위 SK하이닉스 500억)
    assert "코스피 500억원" in lines

    rows = preview_rows(uni, o, only_passed=True)
    assert len(rows) == 4 and all(r[6] == "통과" for r in rows)
    rows_all = preview_rows(uni, o, only_passed=False)
    assert any("시총순위" in r[6] for r in rows_all)


def test_preview_summary_counts_etf():
    from stock_svr.ui.universe_preview import preview_rows, summary_lines

    o = opts(min_price=0, top_n=2)
    uni = build_universe(rows_basic() + rows_etf(), o, updated_at=NOW)
    lines = "\n".join(summary_lines(uni, o))
    assert "총 6종목" in lines and "ETF 2종목" in lines
    assert "ETF 200억원" in lines          # ETF 2위(TIGER 미국S&P500) 시총이 컷오프
    rows = preview_rows(uni, o, only_passed=True)
    assert "ETF" in [r[3] for r in rows]


def test_preview_summary_combined_label_with_etf():
    from stock_svr.ui.universe_preview import summary_lines

    o = opts(min_price=0, rank_scope="combined", top_n=4)
    uni = build_universe(rows_basic() + rows_etf(), o, updated_at=NOW)
    assert "코스피+코스닥+ETF" in "\n".join(summary_lines(uni, o))


def test_preview_rows_search_and_limit():
    from stock_svr.ui.universe_preview import preview_rows

    o = opts(min_price=0)
    uni = build_universe(rows_basic(), o, updated_at=NOW)
    assert [r[1] for r in preview_rows(uni, o, search="삼성")] == ["005930"]
    assert [r[1] for r in preview_rows(uni, o, search="000660")] == ["000660"]
    assert len(preview_rows(uni, o, limit=2)) == 2


def test_dialog_summary_with_universe():
    from stock_svr.ui.auto_trade_dialog import collect_start_info

    db = FakeDb()
    db.balance = {"prsm_dpst_aset_amt": 187_200, "snapshot_at": NOW}
    db.algorithms = [
        {"id": 1, "code": "risk_guard", "name": "리스크 가드", "role": "risk", "is_locked": 1,
         "is_enabled": 1, "priority": 1, "param_defs": [], "params": {
             "max_total_invest": "1000000", "max_total_invest_pct": "100",
             "max_invest_per_stock": "300000", "max_invest_per_stock_pct": "10",
             "stop_loss_pct": "-15", "daily_loss_limit_pct": "-3",
             "trade_start_time": "09:05", "trade_end_time": "15:15"}},
        {"id": 7, "code": "universe_filter", "name": "유니버스", "role": "filter", "is_locked": 0,
         "is_enabled": 1, "priority": 35, "param_defs": DEFS, "params": {}},
    ]
    info = collect_start_info(db, account_id=1)
    assert info["universe_on"] is True
    assert "코스피+코스닥+ETF" in info["universe_text"]
    assert "시총 상위 100" in info["universe_text"]
    assert "최소 주가 50,000원" in info["universe_text"]
    assert info["limit_asset_text"] == "187,200원"
    assert "18,720원" in info["limit_per_text"]
    assert "매수할 수 없습니다" in info["limit_warning"]


def test_dialog_summary_without_universe():
    from stock_svr.ui.auto_trade_dialog import collect_start_info

    db = FakeDb()
    db.algorithms = [
        {"id": 1, "code": "risk_guard", "name": "리스크 가드", "role": "risk", "is_locked": 1,
         "is_enabled": 1, "priority": 1, "param_defs": [], "params": {
             "max_total_invest": "1000000", "max_invest_per_stock": "300000"}},
    ]
    info = collect_start_info(db)
    assert info["universe_on"] is False
    assert "미사용" in info["universe_text"]
    assert info["limit_asset_text"] == "확인 불가"
    assert info["limit_warning"] == ""


def test_seed_param_defs_match_options():
    """seed.sql 정의(DEFS)로 만든 기본값이 코드 기본값과 같은지."""
    o = UniverseOptions.from_params(ParamSet(DEFS, {}))
    assert (o.use_kospi, o.use_kosdaq, o.use_etf) == (True, True, True)
    assert (o.rank_scope, o.top_n) == ("per_market", 100)
    assert o.markets_text == "코스피+코스닥+ETF"
    assert (o.min_price, o.max_price, o.min_market_cap_eok) == (50_000, 0, 0)
    assert (o.exclude_preferred, o.exclude_spac, o.exclude_warning) == (True, True, True)
    assert (o.apply_to, o.stale_days) == ("entry", 5)
    assert o.errors() == []


# ====================================================================== #
# 11. 파이프라인 — filter 단계에서 risk_guard 앞에 동작
# ====================================================================== #
def test_pipeline_blocks_before_risk_guard(monkeypatch):
    """runner 파이프라인에서 universe_filter 가 매수 신호를 걸러낸다."""
    from stock_svr.algo import registry
    from stock_svr.algo.base import Algorithm

    class _Entry(Algorithm):
        code = "_test_entry"
        role = "entry"

        def evaluate(self, ctx):
            return [buy("035760"), buy("005930")]

    registry.register(_Entry)
    try:
        db = db_with(rows_basic())
        metas = [
            {"id": 1, "code": "_test_entry", "name": "진입", "role": "entry", "is_locked": 0,
             "is_enabled": 1, "priority": 10, "param_defs": [], "params": {}},
            {"id": 2, "code": "universe_filter", "name": "유니버스", "role": "filter",
             "is_locked": 0, "is_enabled": 1, "priority": 35, "param_defs": DEFS,
             "params": {"top_n": "1", "min_price": "0"}},
        ]
        algos = registry.build_all(metas)
        filters = [a for a in algos if a.role == "filter"]
        ctx = make_ctx(db, now=NOW)
        signals = []
        for a in algos:
            signals += a.evaluate(ctx)
        for f in filters:
            signals = f.filter_signals(ctx, signals)
        assert [s.stk_cd for s in signals] == ["005930"]
        assert db.signals[-1]["signal_type"] == "BLOCK"
        assert db.signals[-1]["algo_code"] == "universe_filter"
    finally:
        registry._REGISTRY.pop("_test_entry", None)      # noqa: SLF001
