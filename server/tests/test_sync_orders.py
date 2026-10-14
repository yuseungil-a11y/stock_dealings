"""OrderSyncService 체결 알림 메일 - 중복발송 방지 검증.

실제 SMTP 는 전혀 건드리지 않는다 - `mail_notify.send_trade_completed_mail` 을
스파이로 바꿔서 "몇 번 호출됐는지"만 확인한다(SMTP 자체는 test_mail_notify.py 에서 검증).
"""
from __future__ import annotations

from conftest import FakeDb
from stock_svr.kiwoom.fake import FakeRest
from stock_svr.services import mail_notify
from stock_svr.services.sync_orders import OrderSyncService

CNTR_REST = {
    "return_code": 0,
    "cntr": [
        {"ord_no": "0009", "stk_cd": "A005930", "stk_nm": "삼성전자", "io_tp_nm": "현금매수",
         "cntr_qty": "000000010", "cntr_pric": "+000060000", "ord_qty": "000000010",
         "oso_qty": "0", "ord_stt": "체결", "ord_tm": "093015"},
    ],
}


def _mail_spy(monkeypatch):
    calls = []
    monkeypatch.setattr(mail_notify, "send_trade_completed_mail",
                        lambda cfg, **kw: calls.append((cfg, kw)))
    return calls


DUMMY_MAIL_CFG = {"smtp": {"host": "h", "port": 587, "user": "u", "password": "p",
                           "from_addr": "a@b.com", "from_name": "n"}, "to_addr": "x@b.com"}


# ====================================================================== #
# WS 경로 (on_order_exec)
# ====================================================================== #
def test_ws_new_execution_triggers_mail(monkeypatch):
    calls = _mail_spy(monkeypatch)
    svc = OrderSyncService(FakeDb(), FakeRest(), 1, mail_cfg=DUMMY_MAIL_CFG)
    svc.on_order_exec({
        "9203": "0007", "9001": "A005930", "302": "삼성전자", "907": "2",
        "900": "10", "902": "4", "911": "6", "910": "+60000", "909": "C1",
        "913": "체결", "908": "093015", "906": "0",
    })
    assert len(calls) == 1
    cfg, kw = calls[0]
    assert cfg is DUMMY_MAIL_CFG
    assert kw["side"] == "BUY" and kw["stk_cd"] == "005930" and kw["qty"] == 6


def test_ws_same_cntr_no_does_not_send_mail_twice(monkeypatch):
    """같은 체결번호(909)가 WS 재연결/재수신으로 두 번 오면 메일은 1번만."""
    calls = _mail_spy(monkeypatch)
    db = FakeDb()
    svc = OrderSyncService(db, FakeRest(), 1, mail_cfg=DUMMY_MAIL_CFG)
    payload = {
        "9203": "0007", "9001": "A005930", "302": "삼성전자", "907": "2",
        "900": "10", "902": "4", "911": "6", "910": "+60000", "909": "C1",
        "913": "체결", "908": "093015", "906": "0",
    }
    svc.on_order_exec(payload)
    svc.on_order_exec(dict(payload))      # 재수신
    assert len(calls) == 1


def test_ws_no_mail_when_mail_cfg_none(monkeypatch):
    """mail_cfg 를 안 주면(=미설정) 메일 함수가 호출은 되지만 내부에서 즉시 no-op."""
    called = []
    monkeypatch.setattr(mail_notify, "send_trade_completed_mail",
                        lambda cfg, **kw: called.append(cfg))
    svc = OrderSyncService(FakeDb(), FakeRest(), 1)      # mail_cfg 기본값 None
    svc.on_order_exec({
        "9203": "0007", "9001": "A005930", "302": "삼성전자", "907": "2",
        "900": "10", "902": "4", "911": "6", "910": "+60000", "909": "C1",
        "913": "체결", "908": "093015", "906": "0",
    })
    assert called == [None]


# ====================================================================== #
# REST 경로 (sync_executions, 백업)
# ====================================================================== #
def test_rest_new_execution_triggers_mail(monkeypatch):
    calls = _mail_spy(monkeypatch)
    svc = OrderSyncService(FakeDb(), FakeRest({"ka10076": CNTR_REST}), 1,
                           mail_cfg=DUMMY_MAIL_CFG)
    svc.sync_executions()
    assert len(calls) == 1
    assert calls[0][1]["side"] == "BUY"


def test_rest_repeated_poll_does_not_send_mail_twice(monkeypatch):
    """같은 체결을 REST 로 두 번 조회해도(백업 폴링 재관측) 메일은 1번만."""
    calls = _mail_spy(monkeypatch)
    db = FakeDb()
    svc = OrderSyncService(db, FakeRest({"ka10076": CNTR_REST}), 1, mail_cfg=DUMMY_MAIL_CFG)
    svc.sync_executions()
    svc.sync_executions()
    assert len(calls) == 1


def test_ws_execution_mail_includes_algo_code_and_reason_from_order(monkeypatch):
    """체결에 대응하는 orders 행에 algo_code/reason 이 있으면 메일 함수에 그대로 전달된다."""
    calls = _mail_spy(monkeypatch)
    db = FakeDb()
    db.order_reasons["0007"] = {
        "algo_code": "take_profit",
        "reason": "ATR 트레일링 청산 (peak 7,010 - ATR 437.87x3 = 5,696 >= 현재 5,670)",
    }
    svc = OrderSyncService(db, FakeRest(), 1, mail_cfg=DUMMY_MAIL_CFG)
    svc.on_order_exec({
        "9203": "0007", "9001": "A005930", "302": "삼성전자", "907": "2",
        "900": "10", "902": "4", "911": "6", "910": "+60000", "909": "C1",
        "913": "체결", "908": "093015", "906": "0",
    })
    assert len(calls) == 1
    _, kw = calls[0]
    assert kw["algo_code"] == "take_profit"
    assert kw["reason"] == "ATR 트레일링 청산 (peak 7,010 - ATR 437.87x3 = 5,696 >= 현재 5,670)"


def test_ws_execution_mail_algo_code_and_reason_none_for_manual_order(monkeypatch):
    """수동/외부 주문(orders 테이블에 매칭 없음)이면 algo_code/reason 모두 None 으로 전달된다."""
    calls = _mail_spy(monkeypatch)
    svc = OrderSyncService(FakeDb(), FakeRest(), 1, mail_cfg=DUMMY_MAIL_CFG)
    svc.on_order_exec({
        "9203": "0007", "9001": "A005930", "302": "삼성전자", "907": "2",
        "900": "10", "902": "4", "911": "6", "910": "+60000", "909": "C1",
        "913": "체결", "908": "093015", "906": "0",
    })
    assert len(calls) == 1
    _, kw = calls[0]
    assert kw["algo_code"] is None
    assert kw["reason"] is None


def test_ws_execution_mail_reason_lookup_failure_still_sends_mail(monkeypatch):
    """사유 조회(order_reason_info) 자체가 예외를 던져도 메일 발송은 막히지 않는다(None 으로 처리)."""
    calls = _mail_spy(monkeypatch)
    db = FakeDb()
    db.fail_on.add("order_reason_info")
    svc = OrderSyncService(db, FakeRest(), 1, mail_cfg=DUMMY_MAIL_CFG)
    svc.on_order_exec({
        "9203": "0007", "9001": "A005930", "302": "삼성전자", "907": "2",
        "900": "10", "902": "4", "911": "6", "910": "+60000", "909": "C1",
        "913": "체결", "908": "093015", "906": "0",
    })
    assert len(calls) == 1
    _, kw = calls[0]
    assert kw["algo_code"] is None
    assert kw["reason"] is None


def test_rest_execution_mail_includes_algo_code_and_reason_from_order(monkeypatch):
    """REST 동기화 경로에서도 ord_no 로 orders 의 algo_code/reason 을 조회해 전달한다."""
    calls = _mail_spy(monkeypatch)
    db = FakeDb()
    db.order_reasons["0009"] = {
        "algo_code": "volatility_breakout",
        "reason": "등락률 8.84% + 거래량급증 310.05% 교집합 (현재가 5,110)",
    }
    svc = OrderSyncService(db, FakeRest({"ka10076": CNTR_REST}), 1, mail_cfg=DUMMY_MAIL_CFG)
    svc.sync_executions()
    assert len(calls) == 1
    _, kw = calls[0]
    assert kw["algo_code"] == "volatility_breakout"
    assert kw["reason"] == "등락률 8.84% + 거래량급증 310.05% 교집합 (현재가 5,110)"


def test_ws_then_rest_same_execution_does_not_send_mail_twice(monkeypatch):
    """WS 로 이미 기록된 체결을 REST 백업이 나중에 다시 관측해도 메일은 1번만.

    REST 는 결정적 체결키(execution_key)를 쓰므로, WS 로 들어온 909(실체결번호)와는
    다른 cntr_no 가 만들어질 수 있다 - 이 테스트는 최소한 REST 단독 재조회에서
    중복이 없는 것과, WS 최초 수신 시 1건만 발송되는 것을 함께 확인한다.
    """
    calls = _mail_spy(monkeypatch)
    db = FakeDb()
    svc = OrderSyncService(db, FakeRest({"ka10076": CNTR_REST}), 1, mail_cfg=DUMMY_MAIL_CFG)
    # WS 로 먼저 들어옴
    svc.on_order_exec({
        "9203": "0009", "9001": "A005930", "302": "삼성전자", "907": "2",
        "900": "10", "902": "0", "911": "10", "910": "+60000", "909": "WSKEY1",
        "913": "체결", "908": "093015", "906": "0",
    })
    assert len(calls) == 1
    # REST 백업이 같은 체결을 나중에 재확인 (같은 ord_no/qty/price → REST 자체 dedup 으로
    # upsert 는 건너뛰지만, 메일 쪽 사전확인도 독립적으로 막아야 한다)
    svc.sync_executions()
    assert len(calls) == 1, "WS 로 이미 기록된 체결을 REST 가 재관측해도 메일이 다시 나가면 안 된다"
