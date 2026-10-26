"""보안·안전 리뷰(Spinoza/Socrates) 항목별 회귀 테스트.

실서버 주문 API 는 어떤 경우에도 호출하지 않는다 — 전부 fake 클라이언트/모의 transport.
"""
from __future__ import annotations

import datetime as _dt
import itertools
from decimal import Decimal

import httpx
import pytest

from conftest import FakeDb, make_ctx
from stock_svr.algo.base import Signal
from stock_svr.algo.params import ParamError, ParamSet, validate
from stock_svr.algo.registry import build as build_algo
from stock_svr.algo.risk_guard import RiskGuard
from stock_svr.config import KiwoomConfig
from stock_svr.engine.context import OrderGateState
from stock_svr.engine.executor import UNKNOWN_PREFIX, Executor
from stock_svr.kiwoom.errors import KiwoomApiError, KiwoomHttpError
from stock_svr.kiwoom.fake import FakeRest
from stock_svr.kiwoom.rest import ORDER_API_IDS, KiwoomRest
from stock_svr.kiwoom.ws import login_ok
from stock_svr.services.housekeeping import EOD_MAX_ATTEMPTS, HousekeepingService
from stock_svr.services.sync_orders import execution_key
from stock_svr.ui.settings_tab import gate_turns_on
from tests_support import RISK_DEFS, buy_signal, make_rest


# ====================================================================== #
# S-01 : 주문 API 는 절대 재시도하지 않는다
# ====================================================================== #
@pytest.mark.parametrize("scenario", ["timeout", "http500", "http429", "rc1700", "http401"])
def test_order_api_never_retried(scenario):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if scenario == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if scenario == "http500":
            return httpx.Response(500)
        if scenario == "http429":
            return httpx.Response(429)
        if scenario == "http401":
            return httpx.Response(401)
        return httpx.Response(200, json={"return_code": 1700, "return_msg": "허용 요청 수 초과"})

    rest = make_rest(handler)
    with rest.unlock_orders():
        with pytest.raises((KiwoomHttpError, KiwoomApiError)):
            rest.call("kt10000", {"stk_cd": "005930"})
    assert calls["n"] == 1, f"{scenario}: 주문 POST 가 {calls['n']}회 - 정확히 1회여야 함"
    rest.close()


def test_readonly_api_still_retries():
    """읽기 전용 TR 은 기존대로 재시도한다(대조군)."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"return_code": 0})

    rest = make_rest(handler)
    data, _ = rest.call("kt00001", {"qry_tp": "2"})
    assert data["return_code"] == 0 and calls["n"] == 2
    rest.close()


def test_unknown_state_marks_order_and_blocks_stock():
    """응답 유실 → FAILED + 'UNKNOWN: 접수여부 불명', 해당 종목 신규 주문 차단."""
    db, rest = FakeDb(), FakeRest()
    rest.raise_on["kt10000"] = KiwoomHttpError("kt10000", 0, "ReadTimeout")
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock")
    ex = Executor(db, rest, 1)

    res = ex.submit(ctx, buy_signal())
    assert res.unknown_state is True and res.status == "FAILED"
    assert db.orders[0]["status"] == "FAILED"
    assert UNKNOWN_PREFIX in db.orders[0]["return_msg"]
    assert "005930" in ex.pending_unknown

    again = ex.submit(ctx, buy_signal())
    assert again.skipped and "접수 여부 불명" in again.blocked_reason
    assert rest.order_call_count == 1          # 재전송 없음


def test_rejected_order_is_not_unknown_state():
    """거래소가 거부(return_code != 0)한 건은 '미접수'가 확실하므로 차단하지 않는다."""
    db, rest = FakeDb(), FakeRest()
    rest.raise_on["kt10000"] = KiwoomApiError("kt10000", 1234, "잔고부족")
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock")
    ex = Executor(db, rest, 1)
    res = ex.submit(ctx, buy_signal())
    assert res.unknown_state is False
    assert ex.pending_unknown == {}        # R-03: 종목별 증거 추적 dict


# ====================================================================== #
# S-02 / S-24 : env↔mode 일치, DB 재조회(TOCTOU), 엄격한 mode 파싱
# ====================================================================== #
def test_env_mode_mismatch_blocks_and_alarms():
    db, rest = FakeDb(), FakeRest(env="real")     # REST 는 실전
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock")   # 설정은 모의
    alarms = []
    ex = Executor(db, rest, 1, on_critical=alarms.append)
    res = ex.submit(ctx, buy_signal())
    assert res.sent is False and rest.order_call_count == 0
    assert "환경 불일치" in res.blocked_reason
    assert alarms and "환경 불일치" in alarms[0]


def test_env_mode_match_allows_send():
    db, rest = FakeDb(), FakeRest(env="mock")
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock")
    assert Executor(db, rest, 1).submit(ctx, buy_signal()).sent is True


def test_gate_closed_in_db_after_evaluation_blocks_send():
    """평가 후 누군가 order_enabled 를 0 으로 바꾸면 전송 직전에 막힌다(TOCTOU)."""
    db, rest = FakeDb(), FakeRest()
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock")
    db.settings["order_enabled"] = "0"            # DB 만 변경
    res = Executor(db, rest, 1).submit(ctx, buy_signal())
    assert res.sent is False and rest.order_call_count == 0
    assert "방금 닫힘" in res.blocked_reason


def test_gate_recheck_failure_is_fail_closed():
    db, rest = FakeDb(), FakeRest()
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock")
    db.fail_on.add("get_settings")
    res = Executor(db, rest, 1).submit(ctx, buy_signal())
    assert res.sent is False and rest.order_call_count == 0
    assert "재확인 실패" in res.blocked_reason


@pytest.mark.parametrize("mode", ["REAL", "Real", "MOCK", "Mock", "prod", "", "real2"])
def test_invalid_trading_mode_closes_gate(mode):
    gate = OrderGateState.from_settings(
        {"order_enabled": "1", "trading_mode": mode, "real_trading_confirm": "1"})
    assert gate.valid_mode is False
    assert gate.can_send_order is False
    assert "올바르지 않음" in gate.block_reason()


def test_valid_modes_keep_truth_table():
    """게이트 진리표(유효 모드)는 그대로여야 한다."""
    for enabled, mode, confirm in itertools.product([False, True], ["mock", "real"], [False, True]):
        gate = OrderGateState.from_settings({
            "order_enabled": "1" if enabled else "0", "trading_mode": mode,
            "real_trading_confirm": "1" if confirm else "0"})
        assert gate.can_send_order is (enabled and (mode == "mock" or confirm))


# ====================================================================== #
# S-03 : 게이트 OFF→ON 이 되는 모든 저장에 REAL 확인
# ====================================================================== #
CLOSED = {"order_enabled": "0", "trading_mode": "real", "real_trading_confirm": "0"}


def test_gate_turns_on_requires_confirm_for_real():
    need, _ = gate_turns_on(CLOSED, {"order_enabled": "1", "trading_mode": "real",
                                     "real_trading_confirm": "1"})
    assert need is True


def test_mock_to_real_switch_is_caught():
    """우회 시나리오: mock 에서 두 스위치를 켜둔 뒤 real 로만 전환."""
    current = {"order_enabled": "1", "trading_mode": "mock", "real_trading_confirm": "1"}
    need, what = gate_turns_on(current, {**current, "trading_mode": "real"})
    assert need is True and "real" in what


def test_mock_gate_on_does_not_require_confirm():
    need, _ = gate_turns_on(CLOSED, {"order_enabled": "1", "trading_mode": "mock",
                                     "real_trading_confirm": "0"})
    assert need is False


def test_no_confirm_when_gate_stays_off():
    need, _ = gate_turns_on(CLOSED, {**CLOSED, "real_trading_confirm": "1"})
    assert need is False


def test_no_confirm_when_already_on_same_mode():
    current = {"order_enabled": "1", "trading_mode": "real", "real_trading_confirm": "1"}
    need, _ = gate_turns_on(current, dict(current))
    assert need is False


# ====================================================================== #
# S-04 / S-05 : 한도 0 = 주문 금지, 일 손실 한도 비교
# ====================================================================== #
def guard(**over) -> RiskGuard:
    return RiskGuard(meta={"code": "risk_guard"}, params=ParamSet(RISK_DEFS, over))


@pytest.mark.parametrize("key,reason", [
    ("max_total_invest", "총 투입 한도가 0"),
    ("max_invest_per_stock", "종목당 최대 투입금이 0"),
    ("max_orders_per_day", "일 최대 주문 횟수가 0"),
])
def test_zero_limit_blocks_order(key, reason):
    ok, why = guard(**{key: "0"}).check(make_ctx(FakeDb()), buy_signal())
    assert ok is False and reason in why


def test_non_negative_daily_loss_limit_is_rejected():
    ok, why = guard(daily_loss_limit_pct="0").check(make_ctx(FakeDb()), buy_signal())
    assert ok is False and "설정 오류" in why


def test_daily_loss_limit_boundary_blocks():
    db = FakeDb()
    db.realized_pl = -300_000          # 자산 10,000,000 → -3%
    ok, why = guard().check(make_ctx(db), buy_signal())
    assert ok is False and "일 손실 한도" in why


# ====================================================================== #
# S-06 / S-07 / S-08 : fail-closed
# ====================================================================== #
@pytest.mark.parametrize("failing", ["has_open_order", "last_order_at"])
def test_duplicate_check_failure_blocks_order(failing):
    db, rest = FakeDb(), FakeRest()
    db.fail_on.add(failing)
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock")
    res = Executor(db, rest, 1).submit(ctx, buy_signal())
    assert res.skipped and rest.order_call_count == 0
    assert ctx.risk_halt


def test_order_count_query_failure_blocks_buy():
    db = FakeDb()
    db.fail_on.add("count_orders_today")
    ok, why = guard().check(make_ctx(db), buy_signal())
    assert ok is False and "조회 실패" in why


def test_stale_balance_blocks_buy():
    db, rest = FakeDb(), FakeRest()
    now = _dt.datetime(2026, 9, 18, 10, 30)
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock", now=now,
                   balance={"ord_alow_amt": 1_000_000,
                            "snapshot_at": now - _dt.timedelta(minutes=30)})
    res = Executor(db, rest, 1).submit(ctx, buy_signal())
    assert res.skipped and "주문가능금액 확인 불가" in res.blocked_reason
    assert rest.order_call_count == 0


def test_zero_cash_blocks_buy():
    db, rest = FakeDb(), FakeRest()
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock",
                   balance={"ord_alow_amt": 0, "entr": 0})
    res = Executor(db, rest, 1).submit(ctx, buy_signal())
    assert res.skipped and rest.order_call_count == 0


def test_position_state_failure_halts_following_orders():
    db, rest = FakeDb(), FakeRest()
    db.fail_on.add("bump_position_invest")
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock")
    alarms = []
    ex = Executor(db, rest, 1, on_critical=alarms.append)
    first = ex.submit(ctx, buy_signal())
    assert first.sent is True           # 이미 전송된 건은 되돌릴 수 없음
    assert ctx.risk_halt and alarms

    second = ex.submit(ctx, buy_signal(stk="000660"))
    assert second.sent is False and "리스크 판단 불가" in second.blocked_reason
    assert rest.order_call_count == 1


def test_risk_halt_blocks_buy_in_cycle():
    """R-05: `risk_halt` 는 **매수**를 막는다.

    예전에는 같은 사이클의 손절·청산 매도까지 함께 막혔다(아래 test_safety 의
    `test_risk_halt_does_not_block_stop_loss_sell` 참조). 리스크 계산이 불가능할수록
    포지션을 줄이는 매도는 나가야 하므로, 매도 차단은 더 이상 정답이 아니다.
    """
    db, rest = FakeDb(), FakeRest()
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock")
    ctx.halt("테스트 중단")
    res = Executor(db, rest, 1).submit(ctx, buy_signal())
    assert res.sent is False and rest.order_call_count == 0


def test_risk_halt_does_not_block_stop_loss_sell():
    """R-05: 손절 매도는 halt 와 무관하게 (게이트·env 검사를 거쳐) 통과한다."""
    db, rest = FakeDb(), FakeRest()
    holdings = {"005930": {"stk_cd": "005930", "stk_nm": "테스트종목", "rmnd_qty": 10,
                           "trde_able_qty": 10, "cur_prc": 10_000}}
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock", holdings=holdings)
    ctx.halt("테스트 중단")
    sell = Signal(algo_code="risk_guard", stk_cd="005930", side="SELL", qty=10,
                  trde_tp="3", kind="stop_loss", reason="손절")
    res = Executor(db, rest, 1).submit(ctx, sell)
    assert res.sent is True and rest.order_calls[-1][0] == "kt10001"


# ====================================================================== #
# S-10-② : 한 사이클 누적 투입액
# ====================================================================== #
def test_cycle_accumulates_invest_for_total_limit():
    db, rest = FakeDb(), FakeRest()
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock")
    ex = Executor(db, rest, 1)
    ex.submit(ctx, buy_signal(qty=10, price=10000))         # 100,000
    assert ctx.cycle_invested == 100_000
    assert ctx.total_invested() == 100_000
    assert ctx.invested_in("005930") == 100_000


def test_cycle_invest_counts_toward_total_limit():
    db = FakeDb()
    ctx = make_ctx(db, order_enabled=True, trading_mode="mock")
    ctx.cycle_invested = 950_000
    ok, why = guard().check(ctx, buy_signal(qty=10, price=10000))
    assert ok is False and "총 절대 한도" in why


# ====================================================================== #
# S-12 : 시장가 슬리피지 버퍼
# ====================================================================== #
def test_market_order_uses_slippage_buffer():
    sig = buy_signal(qty=28, price=10000)      # 280,000 → ×1.1 = 308,000 > 300,000
    sig.trde_tp = "3"
    ok, why = guard().check(make_ctx(FakeDb()), sig)
    assert ok is False and "종목당 절대 한도" in why


def test_limit_order_has_no_buffer():
    sig = buy_signal(qty=28, price=10000)
    sig.trde_tp = "0"
    ok, _ = guard().check(make_ctx(FakeDb()), sig)
    assert ok is True


# ====================================================================== #
# S-16 / S-19 : 파라미터 범위 재검증 · NaN 거부
# ====================================================================== #
def test_out_of_range_param_disables_algorithm():
    meta = {
        "code": "risk_guard", "name": "가드", "role": "risk",
        "param_defs": [{"param_key": "max_total_invest", "label": "총한도", "value_type": "int",
                        "default_value": "1000000", "min_value": "1", "max_value": "1000000000"}],
        "params": {"max_total_invest": "0"},        # min_value 위반
    }
    assert build_algo(meta) is None


def test_in_range_param_builds_algorithm():
    meta = {
        "code": "risk_guard", "name": "가드", "role": "risk",
        "param_defs": [{"param_key": "max_total_invest", "label": "총한도", "value_type": "int",
                        "default_value": "1000000", "min_value": "1", "max_value": "1000000000"}],
        "params": {"max_total_invest": "500000"},
    }
    assert build_algo(meta) is not None


@pytest.mark.parametrize("bad", ["nan", "NaN", "Infinity", "-Infinity", "inf"])
def test_nan_infinity_rejected(bad):
    pdef = {"param_key": "k", "label": "값", "value_type": "decimal", "default_value": "1",
            "min_value": None, "max_value": None}
    with pytest.raises(ParamError):
        validate(pdef, bad)


# ====================================================================== #
# S-20 : WS LOGIN 은 return_code == 0 명시
# ====================================================================== #
@pytest.mark.parametrize("payload,expected", [
    ({"return_code": 0}, True),
    ({"return_code": "0"}, True),
    ({"return_code": 1}, False),
    ({}, False),                  # 누락은 실패
    ({"return_code": None}, False),
    ({"return_code": "ok"}, False),
])
def test_ws_login_requires_explicit_zero(payload, expected):
    assert login_ok(payload) is expected


# ====================================================================== #
# B1 : 장마감 정리 백오프
# ====================================================================== #
def test_eod_failure_backs_off_and_caps_attempts():
    db = FakeDb()
    house = HousekeepingService(db, rest=None, account_id=1)
    calls = {"n": 0}

    def boom(*a, **kw):
        calls["n"] += 1
        raise RuntimeError("조회 실패")

    house.sync_trade_ledger = boom
    assert house.run_end_of_day() is False
    assert house.run_end_of_day() is False       # 백오프 중 - 재시도 안 함
    assert calls["n"] == 1

    for _ in range(EOD_MAX_ATTEMPTS + 2):        # 백오프 해제 후 상한까지만
        house._eod_next_try = None               # noqa: SLF001
        house.run_end_of_day()
    assert calls["n"] <= EOD_MAX_ATTEMPTS


# -- 휴장일 · 재시작 시 장마감 정리 재실행 방지 ------------------------------ #
class _EodDb(FakeDb):
    def __init__(self):
        super().__init__()
        self.diary_writes: list = []
        self.snapshots: list = []

    def upsert_trade_ledger(self, account_id, rows):
        return len(rows)

    def upsert_daily_summary(self, account_id, base_dt, rows):
        self.diary_writes.append((base_dt, list(rows)))
        return len(rows)

    def snapshot_holdings(self, account_id, snap_date):
        self.snapshots.append(snap_date)
        return 1


def _bars(*dates):
    return {"return_code": 0, "stk_dt_pole_chart_qry": [
        {"dt": d.strftime("%Y%m%d"), "cur_prc": "60000"} for d in dates]}


def _eod_house(monkeypatch, today, bars, db=None):
    from stock_svr.services import housekeeping as hk

    monkeypatch.setattr(hk, "today_kst", lambda: today)
    rest = FakeRest({"ka10081": bars, "kt00015": {"return_code": 0},
                     "ka10170": {"return_code": 0, "tdy_trde_diary": [
                         {"stk_cd": "457370", "sell_qty": "5", "sell_amt": "55450",
                          "pl_amt": "-2270"}]}})
    db = db or _EodDb()
    return HousekeepingService(db, rest, 2), db, rest


def test_eod_skips_weekday_holiday(monkeypatch):
    """한글날(2026-10-09, 금) - 최신 일봉이 10-08 이면 휴장. 매매일지·스냅샷을 쓰지 않는다
    (예전엔 10-08 데이터가 10-09 날짜로 한 벌 더 저장됐다)."""
    holiday = _dt.date(2026, 10, 9)
    house, db, rest = _eod_house(monkeypatch, holiday,
                                 _bars(_dt.date(2026, 10, 7), _dt.date(2026, 10, 8)))
    assert house.run_end_of_day() is False
    assert db.diary_writes == [] and db.snapshots == []
    assert [a for a, _ in rest.calls] == ["ka10081"]
    assert "last_eod_date" not in db.settings
    # 같은 날은 다시 확인하지 않는다(API 반복 호출 없음)
    house._eod_next_try = None                   # noqa: SLF001
    assert house.run_end_of_day() is False
    assert [a for a, _ in rest.calls] == ["ka10081"]


def test_eod_runs_on_trading_day_and_persists_completion(monkeypatch):
    day = _dt.date(2026, 10, 26)
    house, db, rest = _eod_house(monkeypatch, day, _bars(_dt.date(2026, 10, 23), day))
    assert house.run_end_of_day() is True
    assert [d for d, _ in db.diary_writes] == [day] and db.snapshots == [day]
    assert db.settings["last_eod_date"] == "2026-10-26"
    body = dict(rest.calls)["ka10081"]
    assert body["stk_cd"] == "005930" and body["base_dt"] == "20261026"


def test_eod_not_rerun_after_restart_same_day(monkeypatch):
    """재시작(새 HousekeepingService)해도 오늘 이미 끝났으면 키움 호출·덮어쓰기를 하지 않는다."""
    day = _dt.date(2026, 10, 26)
    db = _EodDb()
    db.settings["last_eod_date"] = "2026-10-26"
    house, db, rest = _eod_house(monkeypatch, day, _bars(day), db=db)
    assert house.run_end_of_day() is False
    assert rest.calls == [] and db.diary_writes == [] and db.snapshots == []
    # 전날 기록만 있으면 오늘은 정상 실행
    db2 = _EodDb()
    db2.settings["last_eod_date"] = "2026-10-23"
    house2, db2, _ = _eod_house(monkeypatch, day, _bars(day), db=db2)
    assert house2.run_end_of_day() is True
    assert db2.settings["last_eod_date"] == "2026-10-26"


def test_eod_postponed_when_trading_day_unknown(monkeypatch):
    """개장일 확인 불가(일봉 없음/조회 실패)면 쓰지 않고 백오프 후 다시 확인한다."""
    day = _dt.date(2026, 10, 26)
    house, db, rest = _eod_house(monkeypatch, day, {"return_code": 0})
    assert house.run_end_of_day() is False
    assert db.diary_writes == [] and "last_eod_date" not in db.settings
    assert house.run_end_of_day() is False       # 백오프 중
    assert len(rest.calls) == 1
    rest.responses["ka10081"] = _bars(day)
    house._eod_next_try = None                   # noqa: SLF001
    assert house.run_end_of_day() is True


def test_eod_force_skips_checks(monkeypatch):
    day = _dt.date(2026, 10, 9)
    db = _EodDb()
    db.settings["last_eod_date"] = "2026-10-09"
    house, db, rest = _eod_house(monkeypatch, day, _bars(_dt.date(2026, 10, 8)), db=db)
    assert house.run_end_of_day(force=True) is True
    assert "ka10081" not in [a for a, _ in rest.calls]


# ====================================================================== #
# B2 : 체결 결정적 키
# ====================================================================== #
def test_execution_key_is_deterministic():
    a = execution_key("0001", "093015", 60000, 10)
    b = execution_key("0001", "093015", 60000, 10)
    c = execution_key("0001", "093015", 60000, 11)
    assert a == b and a != c and a.startswith("R") and len(a) == 16


# ====================================================================== #
# B3 : 한도 집계는 실전송만
# ====================================================================== #
def test_order_count_uses_sent_only():
    seen = {}

    class D(FakeDb):
        def count_orders_today(self, account_id, only_sent=True):
            seen["only_sent"] = only_sent
            return 0

    guard().check(make_ctx(D()), buy_signal())
    assert seen["only_sent"] is True


# ====================================================================== #
# 주문 API 목록 방어
# ====================================================================== #
def test_cancel_api_in_order_ids():
    assert {"kt10000", "kt10001", "kt10002", "kt10003"} <= set(ORDER_API_IDS)
