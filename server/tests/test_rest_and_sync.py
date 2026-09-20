"""연속조회·토큰 수명·WS 프로토콜·동기화 파싱 테스트 (Socrates 지적 보강).

네트워크는 httpx MockTransport 로 대체한다. 실서버 호출 없음.
"""
from __future__ import annotations

import datetime as _dt
import json

import httpx
import pytest

from conftest import FakeDb
from stock_svr.algo.params import validate_all
from stock_svr.kiwoom.auth import REFRESH_MARGIN, TokenManager
from stock_svr.kiwoom.errors import KiwoomApiError
from stock_svr.kiwoom.fake import FakeRest
from stock_svr.services.housekeeping import HousekeepingService
from stock_svr.services.sync_orders import OrderSyncService
from tests_support import make_rest


# ====================================================================== #
# 연속조회 (cont-yn / next-key)
# ====================================================================== #
def test_call_paged_follows_next_key():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.headers.get("cont-yn"), request.headers.get("next-key")))
        page = len(seen)
        if page < 3:
            return httpx.Response(200, json={"return_code": 0, "page": page},
                                  headers={"cont-yn": "Y", "next-key": f"KEY{page}"})
        return httpx.Response(200, json={"return_code": 0, "page": page},
                              headers={"cont-yn": "N"})

    rest = make_rest(handler)
    pages = rest.call_paged("kt00018", {"qry_tp": "1"})
    assert [p["page"] for p in pages] == [1, 2, 3]
    assert seen[0] == (None, None)
    assert seen[1] == ("Y", "KEY1") and seen[2] == ("Y", "KEY2")
    rest.close()


def test_call_paged_respects_max_pages():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"return_code": 0},
                              headers={"cont-yn": "Y", "next-key": "K"})

    rest = make_rest(handler)
    assert len(rest.call_paged("kt00018", {}, max_pages=4)) == 4
    rest.close()


def test_call_paged_stops_when_next_key_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"return_code": 0}, headers={"cont-yn": "Y"})

    rest = make_rest(handler)
    assert len(rest.call_paged("kt00018", {})) == 1
    rest.close()


def test_nonzero_return_code_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"return_code": 3, "return_msg": "오류"})

    rest = make_rest(handler)
    with pytest.raises(KiwoomApiError):
        rest.call("kt00001", {})
    rest.close()


# ====================================================================== #
# 토큰 수명 / 재발급
# ====================================================================== #
class _Cfg:
    http_timeout_sec = 1.0

    def domain(self, env):
        return "https://example.invalid"

    def read_keys(self, env):
        return "APPKEY", "SECRET"


def _token_manager(expires: str | None = "20260920120000") -> TokenManager:
    tm = TokenManager(_Cfg(), "mock", 1.0)
    tm._token = "T-1"                                   # noqa: SLF001
    tm._expires_at = (_dt.datetime.strptime(expires, "%Y%m%d%H%M%S")  # noqa: SLF001
                      if expires else None)
    return tm


def test_token_not_expired_when_far_from_expiry():
    tm = _token_manager((_dt.datetime.now() + _dt.timedelta(hours=5)).strftime("%Y%m%d%H%M%S"))
    assert tm._expired() is False                       # noqa: SLF001


def test_token_expired_within_refresh_margin():
    soon = _dt.datetime.now() + (REFRESH_MARGIN / 2)
    tm = _token_manager(soon.strftime("%Y%m%d%H%M%S"))
    assert tm._expired() is True                        # noqa: SLF001


def test_token_masked_reveals_nothing():
    tm = _token_manager()
    masked = tm.masked()
    assert masked == "***"
    assert "T-1" not in masked and "len" not in masked


def test_invalidate_clears_token():
    tm = _token_manager()
    tm.invalidate()
    assert tm.has_token is False


def test_missing_expires_dt_falls_back_to_23h(monkeypatch):
    """B10: expires_dt 를 못 읽으면 발급 + 23시간으로 보수 설정."""
    tm = TokenManager(_Cfg(), "mock", 1.0)

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"return_code": 0, "token": "TK", "expires_dt": "???"}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr("stock_svr.kiwoom.auth.httpx.Client", _Client)
    tm._issue()                                          # noqa: SLF001
    delta = tm.expires_at - tm.issued_at
    assert _dt.timedelta(hours=22, minutes=50) < delta < _dt.timedelta(hours=23, minutes=10)


# ====================================================================== #
# WebSocket PING 에코 / LOGIN
# ====================================================================== #
class _FakeWs:
    """recv/send 만 흉내내는 최소 WS 대역."""

    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent: list[str] = []

    async def recv(self):
        if not self.incoming:
            raise ConnectionError("닫힘")
        return self.incoming.pop(0)

    async def send(self, raw):
        self.sent.append(raw)


def test_ping_is_echoed_verbatim():
    """서버 PING 은 받은 메시지를 그대로 되돌려 보낸다."""
    import asyncio

    from stock_svr.kiwoom.ws import KiwoomWebSocket

    ping = json.dumps({"trnm": "PING", "seq": 7})
    ws = _FakeWs([ping])
    client = KiwoomWebSocket("wss://example.invalid", lambda: "T")
    client._stop.set()                                   # noqa: SLF001

    async def run():
        client._stop.clear()                             # noqa: SLF001
        try:
            await asyncio.wait_for(client._recv_loop(ws), timeout=1)  # noqa: SLF001
        except (ConnectionError, asyncio.TimeoutError):
            pass

    asyncio.run(run())
    assert ws.sent == [ping]


def test_real_message_dispatch():
    from stock_svr.kiwoom.ws import KiwoomWebSocket

    got = []
    client = KiwoomWebSocket("wss://example.invalid", lambda: "T",
                             on_real=lambda t, i, v: got.append((t, i, v)))
    client._dispatch({                                   # noqa: SLF001
        "trnm": "REAL",
        "data": [{"type": "00", "item": "005930", "values": {"9203": "1"}},
                 {"type": "04", "item": "", "values": {"9001": "005930"}},
                 "잘못된 항목"],
    })
    assert [g[0] for g in got] == ["00", "04"]


def test_dispatch_error_does_not_propagate():
    from stock_svr.kiwoom.ws import KiwoomWebSocket

    def boom(*a):
        raise RuntimeError("처리 실패")

    client = KiwoomWebSocket("wss://example.invalid", lambda: "T", on_real=boom)
    client._dispatch({"data": [{"type": "00", "values": {}}]})    # noqa: SLF001  (예외 없음)


# ====================================================================== #
# sync_orders 파싱
# ====================================================================== #
OSO = {
    "return_code": 0,
    "oso": [
        {"ord_no": "0001", "stk_cd": "A005930", "stk_nm": "삼성전자", "ord_qty": "000000010",
         "oso_qty": "000000004", "ord_pric": "+000060000", "ord_stt": "접수",
         "io_tp_nm": "현금매수", "trde_tp": "보통", "stex_tp_txt": "KRX"},
        {"ord_no": "0002", "stk_cd": "000660", "oso_qty": "0"},      # 미체결 0 → 취소대상 아님
    ],
}


def test_fetch_open_orders_filters_zero_remaining():
    svc = OrderSyncService(FakeDb(), FakeRest({"ka10075": OSO}), 1)
    out = svc.fetch_open_orders()
    assert len(out) == 1
    # R-04: UI 취소 대상 목록용으로 종목명·주문수량·방향이 함께 온다
    assert out[0] == {"ord_no": "0001", "stk_cd": "005930", "stk_nm": "삼성전자",
                      "ord_qty": 10, "side": "BUY", "oso_qty": 4, "exchange": "KRX"}


def test_sync_open_orders_computes_filled_qty():
    db = FakeDb()
    captured = {}

    class D(FakeDb):
        def upsert_order_by_ordno(self, account_id, ord_no, **f):
            captured[ord_no] = f
            return 1

    svc = OrderSyncService(D(), FakeRest({"ka10075": OSO}), 1)
    svc.sync_open_orders()
    assert captured["0001"]["filled_qty"] == 6        # B8: ord_qty - oso_qty
    assert captured["0001"]["status"] == "ACCEPTED"
    assert captured["0001"]["stk_cd"] == "005930"
    assert db.orders == []


def test_ws_order_exec_parses_fields():
    captured = {}

    class D(FakeDb):
        def upsert_order_by_ordno(self, account_id, ord_no, **f):
            captured["order"] = (ord_no, f)
            return 1

        def upsert_execution(self, *a, **kw):
            captured["exec"] = a

    svc = OrderSyncService(D(), FakeRest(), 1)
    svc.on_order_exec({
        "9203": "0007", "9001": "A005930", "302": "삼성전자", "907": "2",
        "900": "10", "902": "4", "911": "6", "910": "+60000", "909": "C1",
        "913": "체결", "908": "093015", "906": "0",
    })
    ord_no, fields = captured["order"]
    assert ord_no == "0007" and fields["side"] == "BUY"
    assert fields["stk_cd"] == "005930" and fields["filled_qty"] == 6
    assert captured["exec"][6] == 6 and captured["exec"][7] == 60000


def test_ws_balance_removes_zero_qty_holding():
    calls = []

    class D(FakeDb):
        def execute(self, sql, args=None):
            calls.append((sql, args))
            return 1

    svc = OrderSyncService(D(), FakeRest(), 1)
    svc.on_balance({"9001": "A005930", "930": "0"})
    assert calls and "DELETE FROM holding" in calls[0][0]


# ====================================================================== #
# housekeeping 파싱
# ====================================================================== #
LEDGER = {
    "return_code": 0,
    "trst_ovrl_trde_prps_array": [
        {"trde_dt": "20260918", "trde_no": "000000123", "trde_kind_nm": "현금매수",
         "stk_cd": "A005930", "stk_nm": "삼성전자", "trde_qty_jwa_cnt": "000000010",
         "trde_unit": "+000060000", "trde_amt": "000600000", "cmsn": "000000100",
         "trde_agri_tax": "000000050", "incm_resi_tax": "000000020",
         "exct_amt": "000600170", "entra_remn": "000123456", "proc_tm": "09:30:15"},
        {"trde_dt": "", "trde_no": ""},        # 무효 행은 건너뜀
    ],
}

DIARY = {
    "return_code": 0,
    "tdy_trde_diary": [
        {"stk_cd": "005930", "stk_nm": "삼성전자", "buy_qty": "10", "buy_avg_pric": "60000",
         "buy_amt": "600000", "sell_qty": "5", "sel_avg_pric": "61000", "sell_amt": "305000",
         "cmsn_alm_tax": "200", "pl_amt": "+4800", "prft_rt": "1.60"},
    ],
}


def test_trade_ledger_parsing():
    captured = {}

    class D(FakeDb):
        def upsert_trade_ledger(self, account_id, rows):
            captured["rows"] = rows
            return len(rows)

    HousekeepingService(D(), FakeRest({"kt00015": LEDGER}), 1).sync_trade_ledger()
    rows = captured["rows"]
    assert len(rows) == 1
    r = rows[0]
    assert r["trde_dt"] == _dt.date(2026, 9, 18)
    assert r["stk_cd"] == "005930" and r["trde_unit"] == 60000
    assert r["tax"] == 70          # 거래및농특세 + 소득/주민세


def test_daily_diary_parsing():
    captured = {}

    class D(FakeDb):
        def upsert_daily_summary(self, account_id, base_dt, rows):
            captured["rows"] = rows
            return len(rows)

    HousekeepingService(D(), FakeRest({"ka10170": DIARY}), 1).sync_daily_diary(
        _dt.date(2026, 9, 18))
    r = captured["rows"][0]
    assert r["sell_avg_pric"] == 61000 and r["pl_amt"] == 4800


def test_housekeeping_without_rest_is_noop():
    house = HousekeepingService(FakeDb(), rest=None, account_id=None)
    assert house.sync_trade_ledger() == 0
    assert house.sync_daily_diary() == 0


# ====================================================================== #
# validate_all 경계값
# ====================================================================== #
def _d(**kw):
    base = {"param_key": "k", "label": "값", "value_type": "int", "default_value": "5",
            "min_value": "1", "max_value": "10", "enum_options": None}
    base.update(kw)
    return base


@pytest.mark.parametrize("value,ok", [
    ("1", True), ("10", True), ("0", False), ("11", False), ("-1", False),
])
def test_validate_all_boundaries(value, ok):
    values, errors = validate_all([_d()], {"k": value})
    assert (not errors) is ok
    if ok:
        assert values["k"] == value


def test_validate_all_ignores_unknown_keys():
    values, errors = validate_all([_d()], {"다른키": "99"})
    assert values == {} and errors == []


def test_validate_all_reports_every_error():
    defs = [_d(param_key="a"), _d(param_key="b"), _d(param_key="c")]
    _, errors = validate_all(defs, {"a": "0", "b": "5", "c": "99"})
    assert len(errors) == 2
