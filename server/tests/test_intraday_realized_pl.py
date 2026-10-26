"""장중 실현손익 추정(평균단가) + risk_guard 일 손실 한도 회귀 테스트.

예전 추정치는 '매도체결액 − 매수체결액 − 수수료'(현금흐름)라서 실제 운영에서 이렇게 틀렸다
(event_archive 확인, account_id=2 실계좌):
  * 2026-09-29: '-12.80% 손실'로 보고 매수 1,262건 차단. 실제 그날 손익 -6,337원.
  * 2026-10-16: '-13.82% 손실'로 보고 538건 차단. 실제로는 +4,905원 이익.
  * 2026-10-13: 실제 -40,840원 손실인데 +480,730원으로 보여 한도가 아예 작동하지 않음.
아래 픽스처는 그날의 holding_snapshot(직전 영업일 장마감)과 executions 실데이터를 그대로 옮겼다.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from conftest import FakeDb, make_ctx
from stock_svr.algo import risk_guard as rg_mod
from stock_svr.algo.base import Signal
from stock_svr.algo.params import ParamSet
from stock_svr.algo.risk_guard import RiskGuard
from stock_svr.db import CostBasisUnknownError, Database, _pl_code, avg_cost_realized_pl
from test_risk_guard import DEFS


def _ex(day, hms, stk, side, qty, price, cmsn, tax):
    h, m, s = (int(x) for x in hms.split(":"))
    return {"stk_cd": stk, "side": side, "cntr_qty": qty, "cntr_pric": price,
            "cmsn": cmsn, "tax": tax, "executed_at": _dt.datetime.combine(day, _dt.time(h, m, s))}


D0929 = _dt.date(2026, 9, 29)
D1013 = _dt.date(2026, 10, 13)
D1016 = _dt.date(2026, 10, 16)

# 날짜: (직전 영업일 스냅샷 {종목: (수량, 평균매입가)}, 당일 체결(시간순), ka10170 확정 손익, 추정예탁자산)
CASES = {
    D0929: (
        {"006730": (8, 11450), "040670": (200, 1805), "051370": (13, 7390),
         "181710": (1, 58400), "452190": (19, 5120), "484590": (14, 7070)},
        [_ex(D0929, "09:09:48", "099750", "BUY", 12, 8260, 10, 0),
         _ex(D0929, "09:19:47", "452190", "SELL", 5, 5920, 0, 59),
         _ex(D0929, "14:35:48", "099750", "SELL", 12, 7420, 10, 178),
         _ex(D0929, "14:38:31", "457370", "BUY", 8, 11490, 10, 0),
         _ex(D0929, "14:38:32", "083450", "BUY", 1, 52700, 0, 0)],
        -6337, 537_000),
    D1013: (
        {"006730": (8, 11450), "040670": (200, 1805), "051370": (13, 7390),
         "083450": (1, 52700), "083500": (6, 16240), "091590": (15, 6600),
         "092870": (1, 57000), "181710": (1, 58400), "192400": (3, 27400),
         "323350": (9, 10670), "396300": (16, 6180), "452190": (14, 5120),
         "484590": (14, 7070)},
        [_ex(D1013, "09:06:11", "051370", "SELL", 13, 6560, 10, 170),
         _ex(D1013, "09:06:12", "083500", "SELL", 6, 14400, 10, 172),
         _ex(D1013, "09:06:12", "181710", "SELL", 1, 52400, 0, 104),
         _ex(D1013, "10:33:47", "452190", "SELL", 14, 5670, 10, 158),
         _ex(D1013, "10:49:46", "484590", "SELL", 14, 6370, 10, 178),
         _ex(D1013, "11:25:06", "091590", "SELL", 15, 5940, 10, 178)],
        -40840, 975_000),
    D1016: (
        {"006730": (8, 11450), "012750": (1, 95500), "032640": (6, 15010),
         "040670": (200, 1805), "176750": (12, 8290), "192400": (3, 27400)},
        [_ex(D1016, "09:04:47", "176750", "SELL", 3, 9320, 0, 55),
         _ex(D1016, "09:07:58", "003200", "BUY", 8, 11390, 10, 0),
         _ex(D1016, "09:17:36", "0015N0", "BUY", 10, 9680, 10, 0),
         _ex(D1016, "09:20:40", "006730", "SELL", 2, 12430, 0, 49),
         _ex(D1016, "15:17:01", "003200", "SELL", 8, 11410, 10, 181),
         _ex(D1016, "15:19:59", "059100", "BUY", 2, 8130, 0, 0),
         _ex(D1016, "15:19:59", "059100", "BUY", 12, 8138, 10, 0),
         _ex(D1016, "15:30:08", "396300", "BUY", 11, 9010, 10, 0)],
        4905, 980_000),
}


def _cashflow(executions) -> int:
    """예전(버그) 추정식 - 비교용."""
    sell = sum(e["cntr_qty"] * e["cntr_pric"] for e in executions if e["side"] == "SELL")
    buy = sum(e["cntr_qty"] * e["cntr_pric"] for e in executions if e["side"] == "BUY")
    fees = sum(e["cmsn"] + e["tax"] for e in executions)
    return 0 if sell <= 0 else sell - buy - fees


# ====================================================================== #
# 순수 계산: 장마감 확정치(ka10170)와 비교
# ====================================================================== #
@pytest.mark.parametrize("day", list(CASES))
def test_avg_cost_estimate_matches_eod_diary(day):
    snap, execs, eod_pl, _asset = CASES[day]
    pl, unknown = avg_cost_realized_pl(snap, execs, day)
    assert unknown == []
    assert (pl >= 0) == (eod_pl >= 0)                # 손익 방향 일치
    assert abs(pl - eod_pl) <= 50                    # 매수수수료 차이 정도(수십 원)
    # 예전 현금흐름 추정은 방향·크기 모두 틀렸다(회귀 방지를 위한 기록)
    assert abs(_cashflow(execs) - eod_pl) > 100_000


def test_estimate_never_flips_sign_intraday_on_incident_days():
    """장중 어느 시점에서도 '매수를 많이 했다'는 이유만으로 손실로 바뀌지 않는다."""
    snap, execs, _eod, _asset = CASES[D1016]
    for i in range(1, len(execs) + 1):
        pl, _ = avg_cost_realized_pl(snap, execs[:i], D1016)
        assert pl > 0, f"{i}번째 체결 후 {pl}"


def test_prior_days_executions_roll_position_forward():
    """스냅샷이 며칠 전 것이어도 그 사이 체결로 보유·평단을 이어 붙인다."""
    d0, d1 = _dt.date(2026, 10, 1), _dt.date(2026, 10, 2)
    execs = [_ex(d0, "10:00:00", "111111", "BUY", 10, 1000, 0, 0),   # 스냅샷 이후 매수
             _ex(d1, "10:00:00", "111111", "SELL", 10, 1100, 0, 0)]
    pl, unknown = avg_cost_realized_pl({}, execs, d1)
    assert (pl, unknown) == (1000, [])


def test_buy_after_snapshot_updates_average():
    d = _dt.date(2026, 10, 2)
    execs = [_ex(d, "09:10:00", "111111", "BUY", 10, 2000, 0, 0),    # 평단 (10×1000+10×2000)/20=1500
             _ex(d, "10:00:00", "111111", "SELL", 20, 1400, 5, 50)]
    pl, _ = avg_cost_realized_pl({"111111": (10, 1000)}, execs, d)
    assert pl == 20 * (1400 - 1500) - 55


def test_alnum_code_with_a_prefix_matches_executions():
    """보유표 'A0015N0' ↔ 체결표 '0015N0' 을 같은 종목으로 본다(실데이터 형식)."""
    assert _pl_code("A0015N0") == "0015N0" == _pl_code("0015N0")
    assert _pl_code("A005930") == "005930" and _pl_code("005930_NX") == "005930"
    d = _dt.date(2026, 10, 20)
    pl, unknown = avg_cost_realized_pl(
        {_pl_code("A0015N0"): (10, 9680)},
        [_ex(d, "10:00:00", "0015N0", "SELL", 10, 9780, 0, 0)], d)
    assert (pl, unknown) == (1000, [])


def test_unknown_cost_basis_uses_fallback_then_reports():
    d = _dt.date(2026, 10, 20)
    execs = [_ex(d, "10:00:00", "222222", "SELL", 5, 1200, 0, 0)]
    assert avg_cost_realized_pl({}, execs, d) == (0, ["222222"])
    assert avg_cost_realized_pl({}, execs, d, {"222222": 1000.0}) == (1000, [])


def test_zero_avg_price_in_snapshot_is_not_trusted():
    """스냅샷 평단이 0/NULL 이면 원가 0(=거대한 가짜 이익)으로 쓰지 않고 '미확인'으로 본다."""
    d = _dt.date(2026, 10, 20)
    pl, unknown = avg_cost_realized_pl({"333333": (5, 0)},
                                       [_ex(d, "10:00:00", "333333", "SELL", 5, 1000, 0, 0)], d)
    assert unknown == ["333333"]


# ====================================================================== #
# Database.today_realized_pl_from_executions (SQL 조립·스냅샷 선택·fail-closed)
# ====================================================================== #
class _StubDatabase(Database):
    """커넥션 없이 query/scalar 만 흉내내 실제 메서드 로직을 돌린다."""

    def __init__(self, snaps: dict, executions: list[dict], holdings: list[dict] | None = None):
        self.snaps = snaps                 # {snap_date: {stk_cd: (qty, pur_pric)}}
        self.execs = executions
        self.holdings = holdings or []

    def scalar(self, sql, args=None, default=None):
        assert "MAX(snap_date)" in sql
        before = args[1]
        cands = [d for d in self.snaps if d < before]
        return max(cands) if cands else default

    def query(self, sql, args=None):
        if "FROM holding_snapshot" in sql:
            return [{"stk_cd": c, "rmnd_qty": q, "pur_pric": p}
                    for c, (q, p) in self.snaps[args[1]].items()]
        if "FROM holding " in sql:
            return list(self.holdings)
        assert "FROM executions" in sql
        lo, hi = args[1], args[2]
        lo = lo if isinstance(lo, _dt.datetime) else _dt.datetime.combine(lo, _dt.time())
        hi = hi if isinstance(hi, _dt.datetime) else _dt.datetime.combine(hi, _dt.time())
        return [dict(e) for e in self.execs if lo <= e["executed_at"] < hi]


@pytest.mark.parametrize("day", list(CASES))
def test_database_method_on_incident_days(day):
    snap, execs, eod_pl, _asset = CASES[day]
    prev = day - _dt.timedelta(days=1 if day.weekday() else 3)
    db = _StubDatabase({prev: snap}, execs)
    assert abs(db.today_realized_pl_from_executions(2, day) - eod_pl) <= 50


def test_database_method_no_sells_is_zero():
    d = D1016
    db = _StubDatabase({}, [_ex(d, "09:00:00", "111111", "BUY", 10, 1000, 10, 0)])
    assert db.today_realized_pl_from_executions(2, d) == 0


def test_database_method_fails_closed_on_unknown_cost_basis():
    d = D1016
    db = _StubDatabase({}, [_ex(d, "10:00:00", "999999", "SELL", 5, 1000, 0, 0)])
    with pytest.raises(CostBasisUnknownError):
        db.today_realized_pl_from_executions(2, d)


def test_database_method_falls_back_to_current_holding_avg():
    d = D1016
    db = _StubDatabase({}, [_ex(d, "10:00:00", "999999", "SELL", 5, 1000, 0, 0)],
                       holdings=[{"stk_cd": "999999", "pur_pric": 1100}])
    assert db.today_realized_pl_from_executions(2, d) == -500


# ====================================================================== #
# risk_guard.check 회귀: 그날 그 시각에 매수 신호가 왔다면
# ====================================================================== #
class _IncidentDb(FakeDb):
    def __init__(self, day, upto: _dt.time):
        super().__init__()
        self.day = day
        snap, execs, _eod, _asset = CASES[day]
        self.snap = snap
        self.execs = [e for e in execs if e["executed_at"].time() <= upto]

    def today_realized_pl_from_executions(self, account_id, base_dt):
        pl, unknown = avg_cost_realized_pl(self.snap, self.execs, self.day)
        if unknown:
            raise CostBasisUnknownError(",".join(unknown))
        return pl


def _check_at(day, hh, mm, ss=0, monkeypatch=None):
    monkeypatch.setattr(rg_mod, "today_kst", lambda: day)
    _snap, _execs, _eod, asset = CASES[day]
    now = _dt.datetime.combine(day, _dt.time(hh, mm, ss))
    db = _IncidentDb(day, now.time())
    ctx = make_ctx(db, now=now, balance={"entr": asset, "ord_alow_amt": asset,
                                         "prsm_dpst_aset_amt": asset})
    guard = RiskGuard(meta={"code": "risk_guard", "name": "리스크 가드"},
                      params=ParamSet(DEFS, {"daily_loss_limit_pct": "-3"}))
    sig = Signal(algo_code="momentum_screen", stk_cd="005930", side="BUY", qty=1,
                 price=10000, amount=10000, reason="회귀")
    return guard.check(ctx, sig), ctx


@pytest.mark.parametrize("hms", [(9, 20, 0), (14, 39, 0), (15, 10, 0)])
def test_0929_not_blocked_by_daily_loss(hms, monkeypatch):
    """9/29: 예전엔 09:19 이후 '-12.80%'로 하루 종일 차단. 실제 손익은 -1.2% 수준."""
    (ok, reason), _ = _check_at(D0929, *hms, monkeypatch=monkeypatch)
    assert ok, reason


@pytest.mark.parametrize("hms", [(9, 8, 0), (9, 21, 0), (15, 14, 0)])
def test_1016_profitable_day_not_blocked(hms, monkeypatch):
    """10/16: 실제 +4,905원 이익인 날 - 예전엔 '-13.82%'로 차단."""
    (ok, reason), _ = _check_at(D1016, *hms, monkeypatch=monkeypatch)
    assert ok, reason


def test_1013_loss_limit_engages_after_losses(monkeypatch):
    """10/13: 실제 -40,840원(-4.2%). 예전엔 +480,730원으로 보여 한도가 작동하지 않았다."""
    (ok, reason), _ = _check_at(D1013, 9, 10, monkeypatch=monkeypatch)    # -2.90% → 통과
    assert ok, reason
    (ok, reason), _ = _check_at(D1013, 10, 50, monkeypatch=monkeypatch)   # -3.15% → 차단
    assert not ok and "일 손실 한도" in reason
    (ok, reason), _ = _check_at(D1013, 11, 30, monkeypatch=monkeypatch)   # -4.19% → 차단
    assert not ok and "일 손실 한도" in reason


def test_unknown_cost_basis_blocks_buys_fail_closed(monkeypatch):
    day = D1016
    monkeypatch.setattr(rg_mod, "today_kst", lambda: day)

    class Db(FakeDb):
        def today_realized_pl_from_executions(self, account_id, base_dt):
            raise CostBasisUnknownError("매도 종목 매입단가 확인 불가: 999999")

    ctx = make_ctx(Db(), now=_dt.datetime(2026, 10, 16, 10, 0))
    ok, reason = RiskGuard(meta={"code": "risk_guard", "name": "리스크 가드"},
                           params=ParamSet(DEFS, {})).check(
        ctx, Signal(algo_code="x", stk_cd="005930", side="BUY", qty=1, price=10000, amount=10000))
    assert not ok and "추정 불가" in reason and "999999" in reason
    assert ctx.risk_halt


def test_large_positive_estimate_does_not_mask_recorded_loss(monkeypatch):
    """'절댓값 큰 쪽' 규칙이 아니라 더 불리한 값을 쓴다."""
    monkeypatch.setattr(rg_mod, "today_kst", lambda: D1016)

    class Db(FakeDb):
        def today_realized_pl_from_executions(self, account_id, base_dt):
            return 900_000

    db = Db()
    db.realized_pl = -400_000          # 자산 1천만 대비 -4%
    ok, reason = RiskGuard(meta={"code": "risk_guard", "name": "리스크 가드"},
                           params=ParamSet(DEFS, {})).check(
        make_ctx(db), Signal(algo_code="x", stk_cd="005930", side="BUY", qty=1, price=10000,
                             amount=10000))
    assert not ok and "일 손실 한도" in reason
