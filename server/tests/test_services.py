"""서비스/유틸 단위테스트 (fake 클라이언트만 사용)."""
from __future__ import annotations

import datetime as _dt

import pytest

from conftest import FakeDb
from stock_svr.kiwoom.fake import FakeRest
from stock_svr.kiwoom.ratelimit import RateLimiter
from stock_svr.kiwoom.rest import API_PATHS, ORDER_API_IDS
from stock_svr.logging_setup import _category_of, purge_old_logs
from stock_svr.services.sync_account import AccountService
from stock_svr.services.sync_orders import map_status
from stock_svr.util import is_market_open


# ====================================================================== #
# 장 운영시간
# ====================================================================== #
@pytest.mark.parametrize("ts,expected", [
    (_dt.datetime(2026, 9, 18, 9, 0), True),      # 금 09:00
    (_dt.datetime(2026, 9, 18, 15, 30), True),    # 금 15:30
    (_dt.datetime(2026, 9, 18, 8, 59), False),
    (_dt.datetime(2026, 9, 18, 15, 31), False),
    (_dt.datetime(2026, 9, 19, 10, 0), False),    # 토요일
    (_dt.datetime(2026, 9, 20, 10, 0), False),    # 일요일
])
def test_is_market_open(ts, expected):
    assert is_market_open(ts) is expected


# ====================================================================== #
# 주문 상태 매핑
# ====================================================================== #
@pytest.mark.parametrize("text,oso,filled,ord_qty,expected", [
    ("접수", 10, 0, 10, "ACCEPTED"),
    ("체결", 0, 10, 10, "FILLED"),
    ("체결", 5, 5, 10, "PARTIAL"),
    ("취소", 0, 0, 10, "CANCELED"),
    ("거부", 0, 0, 10, "REJECTED"),
    ("", 0, 10, 10, "FILLED"),
    ("", 0, 3, 10, "PARTIAL"),
])
def test_map_status(text, oso, filled, ord_qty, expected):
    assert map_status(text, oso, filled, ord_qty) == expected


# ====================================================================== #
# 계좌 서비스 (fake 응답)
# ====================================================================== #
KT00018 = {
    "return_code": 0,
    "tot_pur_amt": "000001000000",
    "tot_evlt_amt": "000001050000",
    "tot_evlt_pl": "+50000",
    "tot_prft_rt": "5.00",
    "prsm_dpst_aset_amt": "000002050000",
    "acnt_evlt_remn_indv_tot": [
        {"stk_cd": "A005930", "stk_nm": "삼성전자", "rmnd_qty": "000000010",
         "trde_able_qty": "000000010", "pur_pric": "000060000", "cur_prc": "+000063000",
         "pur_amt": "000600000", "evlt_amt": "000630000", "evltv_prft": "+30000",
         "prft_rt": "5.00", "poss_rt": "30.00"},
    ],
}


def test_fetch_account_no():
    rest = FakeRest({"ka00001": {"return_code": 0, "acctNo": "1234567890"}})
    assert AccountService(FakeDb(), rest).fetch_account_no() == "1234567890"


def test_fetch_deposit_parses_padded_numbers():
    rest = FakeRest({"kt00001": {"return_code": 0, "entr": "000001234567",
                                 "ord_alow_amt": "000000500000", "d2_entra": "-000000100"}})
    dep = AccountService(FakeDb(), rest).fetch_deposit()
    assert dep["entr"] == 1234567
    assert dep["ord_alow_amt"] == 500000
    assert dep["d2_entra"] == -100


def test_fetch_evaluation_normalizes_codes():
    rest = FakeRest({"kt00018": KT00018})
    summary, holdings = AccountService(FakeDb(), rest).fetch_evaluation()
    assert summary["tot_evlt_pl"] == 50000
    assert len(holdings) == 1
    h = holdings[0]
    assert h["stk_cd"] == "005930"          # A 접두 제거
    assert h["cur_prc"] == 63000 and h["rmnd_qty"] == 10


def test_account_service_never_calls_order_api():
    rest = FakeRest({"ka00001": {"return_code": 0, "acctNo": "1"},
                     "kt00001": {"return_code": 0}, "kt00018": KT00018})
    svc = AccountService(FakeDb(), rest)
    svc.fetch_account_no()
    svc.fetch_deposit()
    svc.fetch_evaluation()
    assert rest.order_call_count == 0
    assert all(api not in ORDER_API_IDS for api, _ in rest.calls)


# ====================================================================== #
# Rate limiter
# ====================================================================== #
def test_rate_limiter_waits_between_calls():
    slept: list[float] = []
    clock = {"t": 0.0}
    rl = RateLimiter(0.5)
    rl._sleep = lambda s: (slept.append(s), clock.__setitem__("t", clock["t"] + s))
    rl._clock = lambda: clock["t"]
    rl.acquire()
    rl.acquire()
    assert slept and slept[-1] == pytest.approx(0.5, abs=1e-6)


def test_rate_limiter_backoff_grows_and_relaxes():
    rl = RateLimiter(0.1, max_backoff_sec=8)
    assert rl.penalize() == 1.0
    assert rl.penalize() == 2.0
    assert rl.penalize() == 4.0
    rl.relax()
    assert rl.penalty == 2.0


# ====================================================================== #
# 로깅
# ====================================================================== #
def test_log_category_mapping():
    assert _category_of("stock_svr.kiwoom.ws") == "ws"
    assert _category_of("stock_svr.kiwoom.auth") == "auth"
    assert _category_of("stock_svr.services.sync_account") == "sync"
    assert _category_of("stock_svr.engine.runner") == "engine"
    assert _category_of("stock_svr.algo.risk_guard") == "algo"
    assert _category_of("stock_svr.main") == "system"


def test_purge_old_logs(tmp_path):
    # B13: 고정 날짜를 쓰면 미래에 실패하는 '시한폭탄' 테스트가 되므로 오늘 기준으로 만든다
    today = _dt.date.today()
    keep = tmp_path / f"stock_svr.log.{(today - _dt.timedelta(days=1)).isoformat()}"
    old = tmp_path / f"stock_svr.log.{(today - _dt.timedelta(days=30)).isoformat()}"
    keep.write_text("x", encoding="utf-8")
    old.write_text("x", encoding="utf-8")
    removed = purge_old_logs(tmp_path, retention_days=7)
    assert removed == 1
    assert old.exists() is False and keep.exists() is True


# ====================================================================== #
# API 경로 테이블
# ====================================================================== #
def test_readonly_api_paths_registered():
    for api_id in ("au10001", "ka00001", "kt00001", "kt00018", "ka10075", "ka10076",
                   "ka10099", "ka10027", "ka10023", "ka10081", "ka10001", "kt00015",
                   "ka10170"):
        if api_id.startswith("au"):
            continue
        assert api_id in API_PATHS, api_id
